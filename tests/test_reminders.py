"""模拟时钟/通知器的逻辑测试，不发送真实桌面通知。"""
from contextlib import redirect_stdout, redirect_stderr
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from course_assistant.cli import main
from course_assistant.errors import StorageError, ValidationError
from course_assistant.models import Deadline, TaskReview
from course_assistant.notifications import Delivery, Notifier
from course_assistant.preferences import DEFAULT_REMINDERS, validate_preferences
from course_assistant.reminders import DeadlineReminderPlanner, ReminderRunner, read_state, upcoming
from course_assistant.service import TaskService, validate_config


def at(value): return datetime.fromisoformat(value + "+08:00")


class FakeNotifier:
    def __init__(self): self.calls = []
    def send(self, title, message):
        self.calls.append((title, message))
        return Delivery("terminal", fallback_reason="模拟通知器，不是真实操作系统通知")


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = TaskService(self.root); self.service.initialize()
        self.service.update_preferences(enabled=True, channel="terminal")
        self.fake = FakeNotifier(); self.runner = ReminderRunner(self.service, self.fake)

    def add(self, due="2026-11-20T09:00", course="模拟课程"):
        return self.service.add(course=course, title="【模拟】提醒作业", deadline=Deadline.from_input(due))

    def check(self, time="2026-11-17T09:00", **kwargs):
        with redirect_stdout(io.StringIO()): return self.runner.check(now=at(time), **kwargs)

    def test_three_days_before(self):
        self.add(); result = self.check()
        self.assertEqual([e["node"] for e in result["events"]], ["lead:3"])
        self.assertEqual(len(self.fake.calls), 1)

    def test_one_day_before_after_three_day_node(self):
        self.add(); self.check()
        result = self.check("2026-11-19T09:00")
        self.assertEqual([e["node"] for e in result["events"]], ["lead:1"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_overdue_first_check_coalesces_old_leads(self):
        self.add(); result = self.check("2026-11-21T10:00")
        self.assertEqual([e["node"] for e in result["events"]], ["overdue"])
        self.assertEqual(result["superseded_count"], 2)
        self.assertEqual(self.check("2026-11-22T10:00")["count"], 0)

    def test_completed_tasks_never_remind(self):
        task = self.add(); self.service.complete(task.id)
        self.assertEqual(self.check()["count"], 0)
        self.assertEqual(self.fake.calls, [])

    def test_deleted_tasks_never_remind(self):
        task = self.add(); self.service.recoverable_action(task.id, action="delete", confirmed=True)
        self.assertEqual(self.check()["count"], 0)

    def test_date_only_and_unknown_need_information_not_countdown(self):
        self.add("2026-11-20"); self.add(None)
        result = self.check()
        self.assertEqual([e["node"] for e in result["events"]], ["needs_deadline", "needs_deadline"])
        self.assertTrue(all("不生成精确倒计时" in e["message"] for e in result["events"]))
        self.assertEqual(self.check("2026-11-18T09:00")["count"], 0)

    def test_review_deadline_uncertainty_blocks_precise_countdown(self):
        task = self.add()
        with self.service.store.lock():
            current = task.mutate({"review": TaskReview(True, ("deadline",), ("模拟歧义",))}, "task_updated")
            self.service._save_tasks([current], self.service.config())
        self.assertEqual(self.check()["events"][0]["node"], "needs_deadline")
        self.service.set_deadline(task.id, task.deadline)
        self.assertNotIn("deadline", self.service.get(task.id).review.uncertain_fields)
        self.assertEqual(self.check()["events"][0]["node"], "lead:3")

    def test_repeated_checks_and_restart_do_not_repeat(self):
        self.add(); self.check()
        self.runner = ReminderRunner(TaskService(self.root), self.fake)
        self.assertEqual(self.check()["count"], 0)
        self.assertEqual(len(self.fake.calls), 1)

    def test_postponed_deadline_gets_new_plan(self):
        task = self.add(); first = self.check()
        self.service.set_deadline(task.id, Deadline.from_input("2026-11-23T09:00"))
        self.assertEqual(self.check("2026-11-19T09:00")["count"], 0)
        second = self.check("2026-11-20T09:00")
        self.assertEqual(second["events"][0]["node"], "lead:3")
        self.assertNotEqual(first["events"][0]["revision"], second["events"][0]["revision"])

    def test_undo_postponement_does_not_repeat_previously_delivered_original_node(self):
        task = self.add(); self.check()
        self.service.set_deadline(task.id, Deadline.from_input("2026-11-23T09:00"))
        self.service.recoverable_action(task.id, action="undo", confirmed=True)
        self.assertEqual(self.check()["count"], 0)
        self.assertEqual(len(self.fake.calls), 1)

    def test_unrelated_content_change_does_not_repeat_same_node(self):
        task = self.add(); self.check()
        with self.service.store.lock():
            changed = self.service.get(task.id).mutate({"title": "【模拟】更名"}, "task_updated")
            self.service._save_tasks([changed], self.service.config())
        self.assertEqual(self.check()["count"], 0)

    def test_missed_both_nodes_only_catches_latest(self):
        self.add(); result = self.check("2026-11-19T18:00")
        self.assertEqual([e["node"] for e in result["events"]], ["lead:1"])
        self.assertTrue(result["events"][0]["catch_up"])
        self.assertEqual(result["superseded_count"], 1)

    def test_future_nodes_do_not_fire_early(self):
        self.add(); self.assertEqual(self.check("2026-11-17T08:59:59")["count"], 0)

    def test_custom_lead_days_and_course_override(self):
        self.add(course="模拟金融"); self.add(course="模拟统计")
        with self.service.store.lock():
            config = self.service.config()
            config["reminder_preferences"]["course_overrides"] = {"模拟金融": {"enabled": True, "lead_days": [2]}, "模拟统计": {"enabled": False, "lead_days": [3, 1]}}
            self.service.store.write("config.json", validate_config(config))
        self.assertEqual(self.check()["count"], 0)
        self.assertEqual([e["node"] for e in self.check("2026-11-18T09:00")["events"]], ["lead:2"])

    def test_disabled_still_logs_successful_check(self):
        self.add(); self.service.update_preferences(enabled=False)
        result = self.check()
        self.assertFalse(result["enabled"])
        records = [json.loads(line) for line in Path(result["log"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["event"], "run_started")
        self.assertEqual(records[-1]["event"], "run_finished")

    def test_dry_run_logs_simulation_without_consuming_nodes(self):
        self.add(); result = self.check(dry_run=True)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.root / "reminders.json").exists())
        records = [json.loads(line) for line in Path(result["log"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(e["simulated"] and e["dry_run"] for e in records))
        self.assertEqual(self.check()["count"], 1)

    def test_log_retains_effective_and_actual_timestamps_and_messages(self):
        self.add(); result = self.check()
        records = [json.loads(line) for line in Path(result["log"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all("observed_at" in r and "effective_now" in r for r in records))
        plan = next(r for r in records if r["event"] == "plan_created")
        self.assertIn("模拟课程", plan["events"][0]["message"])
        self.assertIsNotNone(self.runner.status()["last_run"])

    def test_failure_after_reservation_is_unknown_and_not_auto_resent(self):
        self.add()
        with patch.object(self.fake, "send", side_effect=RuntimeError("模拟进程中断")):
            with self.assertRaises(RuntimeError): self.check()
        self.assertEqual(len(self.runner.status()["delivery_unknown"]), 1)
        self.assertEqual(self.check()["count"], 0)
        content = "".join(p.read_text(encoding="utf-8") for p in (self.root / "logs" / "reminder-runs").glob("*.jsonl"))
        self.assertIn("run_failed", content)

    def test_corrupt_state_never_resets_or_sends(self):
        self.add(); path = self.root / "reminders.json"; path.write_text('{"broken": true}', encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(StorageError): self.check()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.fake.calls, [])

    def test_failure_to_persist_reservation_prevents_notification(self):
        self.add()
        with patch.object(self.runner, "_save", side_effect=StorageError("模拟磁盘失败")):
            with self.assertRaises(StorageError): self.check()
        self.assertEqual(self.fake.calls, [])

    def test_concurrent_checks_only_deliver_once(self):
        self.add()
        with redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.runner.check(now=at("2026-11-17T09:00")), range(3)))
        self.assertEqual(sum(result["count"] for result in results), 1)
        self.assertEqual(len(self.fake.calls), 1)

    def test_manual_backup_includes_reminder_state_once_present(self):
        self.add(); self.check()
        self.assertEqual(len(self.service.backup()), 4)

    def test_agenda_window_sort_course_and_uncertain_bucket(self):
        late = self.add("2026-11-23T09:00"); early = self.add("2026-11-18T09:00")
        self.add("2026-11-24T09:00"); self.add("2026-11-16T09:00")
        self.add(None); completed = self.add("2026-11-19T09:00"); self.service.complete(completed.id)
        self.add("2026-11-18T09:00", course="其他模拟课程")
        report = upcoming(self.service.list(), at("2026-11-17T09:00"), course="模拟课程")
        self.assertEqual([t["id"] for t in report["tasks"]], [early.id, late.id])
        self.assertEqual(len(report["needs_information"]), 1)

    def test_legacy_config_gets_defaults_without_rewrite(self):
        config = self.service.config(); del config["reminder_preferences"]
        result = validate_config(config)
        self.assertEqual(result["reminder_preferences"], DEFAULT_REMINDERS)

    def test_invalid_preferences_are_rejected(self):
        for change in ({"lead_days": [True]}, {"lead_days": [3, 3]}, {"lead_days": []}, {"daily_check_time": "9:00"}, {"daily_check_time": "25:00"},
                       {"channel": "email"}, {"course_overrides": {"模拟": {"enabled": "yes", "lead_days": [1]}}}):
            with self.subTest(change=change), self.assertRaises(ValidationError): validate_preferences({**DEFAULT_REMINDERS, **change})

    def test_cli_simulated_time_requires_dry_run_and_no_desktop_calls(self):
        self.add(); stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch("course_assistant.reminders.Notifier") as factory:
            code = main(["--data-dir", str(self.root), "remind", "check", "--now", "2026-11-17T09:00:00+08:00"])
            self.assertEqual(code, 1)
            code = main(["--data-dir", str(self.root), "remind", "check", "--now", "2026-11-17T09:00:00+08:00", "--dry-run"])
            self.assertEqual(code, 0)
            factory.assert_not_called()
        self.assertFalse((self.root / "reminders.json").exists())


class NotificationFallbackTests(unittest.TestCase):
    def test_terminal_mode_never_launches_desktop_helper(self):
        with redirect_stdout(io.StringIO()), patch("course_assistant.notifications.subprocess.run") as run:
            result = Notifier("terminal").send("模拟", "模拟消息")
            run.assert_not_called(); self.assertEqual(result.channel, "terminal")

    def test_desktop_error_falls_back_without_claiming_seen(self):
        with redirect_stdout(io.StringIO()), patch("course_assistant.notifications.IS_WINDOWS", True), patch("course_assistant.notifications.subprocess.run", side_effect=OSError("模拟不支持")):
            result = Notifier().send("模拟", "模拟消息")
        self.assertEqual(result.channel, "terminal"); self.assertIsNone(result.user_visible)
