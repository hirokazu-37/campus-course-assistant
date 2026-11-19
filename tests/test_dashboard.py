"""界面适配层逻辑测试；全部任务为临时目录中的模拟数据。"""
from datetime import datetime
import io
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from course_assistant.dashboard import overview, parse_sent_at, preview_notice, remaining
from course_assistant.errors import ValidationError, StorageError
from course_assistant.models import Deadline
from course_assistant.service import TaskService

NOW = datetime.fromisoformat("2026-11-17T09:00:00+08:00")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = TaskService(self.temp.name)
        self.service.initialize()

    def task(self, due="2026-11-20T09:00"):
        return self.service.add(course="【模拟】测试课程", title="【模拟】作业", deadline=Deadline.from_input(due))

    def test_sent_at_never_guesses_today_or_midnight(self):
        self.assertIsNone(parse_sent_at(" "))
        for value in ("明天", "2026-02-30 12:00", "2026-11-17 25:00"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_sent_at(value)

    def test_sent_at_uses_shanghai_or_converts_explicit_offset(self):
        self.assertEqual(parse_sent_at("2026-11-17 09:10"), "2026-11-17T09:10:00+08:00")
        self.assertEqual(parse_sent_at("2026-11-17T01:10:00Z"), "2026-11-17T09:10:00+08:00")

    def test_sent_at_accepts_date_only_without_midnight(self):
        for value in ("2026-11-13", "2026年11月13日", "2026年11月13", "2026/11/13"):
            with self.subTest(value=value):
                self.assertEqual(parse_sent_at(value), "2026-11-13")

    def test_countdown_and_uncertainty(self):
        task = self.task()
        self.assertEqual(remaining(task, NOW), "剩余 3天 0小时")
        self.assertIn("已逾期", remaining(task, datetime.fromisoformat("2026-11-21T09:00:00+08:00")))
        self.assertEqual(remaining(self.task("2026-11-20"), NOW), "待补充截止时间")
        self.assertEqual(remaining(self.task(None), NOW), "待补充截止时间")
        self.assertEqual(remaining(self.service.complete(task.id).task, NOW), "已完成")

    def test_overview_does_not_consume_nodes_or_send(self):
        self.task()
        self.service.update_preferences(enabled=True)
        paths = [self.service.store.root / name for name in ("deadlines.json", "config.json", "reminders.json")]
        before = {p: p.read_bytes() if p.exists() else None for p in paths}
        with patch("course_assistant.notifications.Notifier.send") as send:
            for _ in range(3):
                result = overview(self.service, NOW)
                self.assertEqual(result["reminder_plan"].events[0].node, "lead:3")
            send.assert_not_called()
        self.assertEqual(before, {p: p.read_bytes() if p.exists() else None for p in paths})

    def test_invalid_input_and_disabled_ai_do_not_construct_workflow(self):
        with patch("course_assistant.dashboard.build_workflow") as build:
            for text, sent in (("", ""), ("x" * 20001, ""), ("【模拟】任务", "2026-02-30")):
                with self.assertRaises(ValidationError):
                    preview_notice(self.service, text, sent)
            with self.service.store.lock():
                config = self.service.config(); config["ai_enabled"] = False
                self.service.store.write("config.json", config)
            with self.assertRaises(ValidationError):
                preview_notice(self.service, "【模拟】任务", "")
            build.assert_not_called()

    def test_course_context_includes_registered_and_existing_courses(self):
        self.task(); self.service.add_course("【模拟】统计学")
        with patch("course_assistant.dashboard.build_workflow") as build:
            preview_notice(self.service, "【模拟】任务", "2026-11-17 09:00")
            notice, context = build.return_value.prepare.call_args.args
            self.assertEqual(set(context.course_names), {"【模拟】测试课程", "【模拟】统计学"})
            self.assertEqual(context.reference_time.isoformat(), notice.sent_at)

    def test_complete_reopen_idempotent_preserves_history_and_undo(self):
        task = self.task()
        completed = self.service.set_completed(task.id, True, expected_version=task.updated_at)
        self.assertTrue(completed.changed)
        self.assertFalse(self.service.set_completed(task.id, True).changed)
        reopened = self.service.set_completed(task.id, False, expected_version=completed.task.updated_at)
        self.assertEqual(reopened.task.status, "pending")
        self.assertIsNone(reopened.task.completed_at)
        self.assertEqual(len(reopened.task.history), 3)
        restored = self.service.recoverable_action(task.id, action="undo", confirmed=True)
        self.assertEqual(restored.status, "completed")
        logs = "".join(p.read_text(encoding="utf-8") for p in self.service.store.root.glob("logs/*.jsonl"))
        self.assertIn("task_reopened", logs)

    def test_stale_status_change_rejected_without_overwrite(self):
        task = self.task()
        self.service.set_deadline(task.id, Deadline.from_input("2026-11-25T09:00"))
        before = (self.service.store.root / "deadlines.json").read_bytes()
        with self.assertRaises(ValidationError):
            self.service.set_completed(task.id, True, expected_version=task.updated_at)
        self.assertEqual((self.service.store.root / "deadlines.json").read_bytes(), before)

    def test_deleted_task_and_non_boolean_status_rejected(self):
        task = self.task()
        self.service.recoverable_action(task.id, action="delete", confirmed=True)
        for value in (True, False, "false", 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.service.set_completed(task.id, value)

    def test_status_write_failure_preserves_old_file(self):
        task = self.task()
        path = self.service.store.root / "deadlines.json"; before = path.read_bytes()
        with patch("course_assistant.storage.os.replace", side_effect=OSError("模拟写入失败")):
            with self.assertRaises(StorageError):
                self.service.set_completed(task.id, True)
        self.assertEqual(path.read_bytes(), before)
