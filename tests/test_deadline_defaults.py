"""个人默认23:59：模拟通知/时钟、临时台账；不是真实模型或系统通知实测。"""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from course_assistant.deadline_policy import DEFAULT_DATE_ONLY_TIME, default_clock_note
from course_assistant.errors import ValidationError
from course_assistant.models import Deadline, Notice, TaskReview
from course_assistant.parsing import AINotificationParser, ParserSettings
from course_assistant.preferences import DEFAULT_REMINDERS
from course_assistant.reminders import DeadlineReminderPlanner, deadline_confirmed
from course_assistant.service import TaskService
from tests.parser_fixtures import RAW, FakeClient, context, deadline, response, settings


class DefaultClockParsingTests(unittest.TestCase):
    def analyze(self, spec=None, *, extra="12月2日前", output=None, config=None):
        spec = spec or deadline("absolute", extra)
        output = output or response(deadline=spec)
        client = FakeClient(output)
        config = config or replace(settings(), date_only_deadline_time=DEFAULT_DATE_ONLY_TIME)
        result = AINotificationParser(client, config).analyze(Notice(RAW + extra, "2026-11-13"), context("2026-11-13"))
        return result.drafts[0], client

    def test_plain_date_defaults_to_end_of_day_and_is_eligible_for_reminders(self):
        draft, client = self.analyze()
        self.assertEqual((draft.deadline.date, draft.deadline.time), ("2026-12-02", "23:59"))
        self.assertFalse(draft.deadline.needs_confirmation)
        self.assertNotIn("deadline", draft.review.uncertain_fields)
        self.assertIn(default_clock_note("23:59"), draft.review.notes)
        self.assertEqual(client.calls[0][1]["trusted_context"]["date_only_deadline_time"], "23:59")

    def test_date_with_explicit_year_defaults_the_clock(self):
        draft, _ = self.analyze(deadline("absolute", "2026年12月2日", date="2026-12-02"), extra="2026年12月2日")
        self.assertEqual(draft.deadline.time, "23:59")

    def test_relative_calendar_date_can_default_clock(self):
        draft, _ = self.analyze(deadline("relative_days", "明天", days=1), extra="明天交")
        self.assertEqual((draft.deadline.date, draft.deadline.time), ("2026-11-14", "23:59"))

    def test_explicit_clock_always_wins(self):
        draft, _ = self.analyze(deadline("absolute", "12月2日14:30", time="14:30", time_expression="14:30"), extra="12月2日14:30")
        self.assertEqual(draft.deadline.time, "14:30")
        self.assertNotIn(default_clock_note("23:59"), draft.review.notes)

    def test_ambiguous_evening_not_replaced_with_end_of_day(self):
        draft, _ = self.analyze(deadline("absolute", "12月2日晚上", time_expression="晚上", uncertain=True), extra="12月2日晚上")
        self.assertIsNone(draft.deadline.time)
        self.assertTrue(draft.deadline.needs_confirmation)

    def test_model_cannot_hide_a_nearby_explicit_time_to_get_default(self):
        draft, _ = self.analyze(deadline("absolute", "12月2日"), extra="12月2日下午三点交")
        self.assertIsNone(draft.deadline.time)

    def test_missing_date_not_invented_from_only_a_clock(self):
        draft, _ = self.analyze(deadline("unknown", None, time="20:00", time_expression="20:00", uncertain=True), extra="20:00交")
        self.assertIsNone(draft.deadline.date)
        self.assertIsNone(draft.deadline.time)

    def test_cross_year_conflict_not_cleared_by_default_clock(self):
        draft, _ = self.analyze(deadline("absolute", "1月5日"), extra="1月5日交")
        self.assertEqual(draft.deadline.date, "2026-01-05")
        self.assertIsNone(draft.deadline.time)
        self.assertTrue(any("跨年" in note for note in draft.review.notes))

    def test_date_uncertainty_in_model_notes_preserved(self):
        output = response(deadline=deadline("absolute", "12月2日", uncertain=True), notes=["截止日期可能变化，仍需核对"], uncertain_fields=["deadline"])
        draft, _ = self.analyze(extra="12月2日", output=output)
        self.assertIsNone(draft.deadline.time)
        self.assertIn("deadline", draft.review.uncertain_fields)

    def test_legacy_only_missing_clock_flag_is_resolved_by_user_policy(self):
        note = "截止日期只有“12月2日前”，无具体钟点，故截止时间仍待确认。"
        output = response(deadline=deadline("absolute", "12月2日前", uncertain=True), uncertain_fields=["deadline"], notes=[note])
        draft, _ = self.analyze(output=output)
        self.assertEqual(draft.deadline.time, "23:59")
        self.assertNotIn("deadline", draft.review.uncertain_fields)
        self.assertNotIn(note, draft.review.notes)
        self.assertIn(default_clock_note("23:59"), draft.review.notes)

    def test_rule_can_be_disabled_or_configured(self):
        draft, _ = self.analyze(config=replace(settings(), date_only_deadline_time=None))
        self.assertIsNone(draft.deadline.time)
        draft, _ = self.analyze(config=replace(settings(), date_only_deadline_time="22:00"))
        self.assertEqual(draft.deadline.time, "22:00")

    def test_config_loading_default_and_strict_validation(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "parsing.json"
            document = {"schema_version": 1, "timezone": "Asia/Shanghai", "semester_week1_monday": None}
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(ParserSettings.load(path).date_only_deadline_time, "23:59")
            for value in (None, "22:15"):
                document["date_only_deadline_time"] = value
                path.write_text(json.dumps(document), encoding="utf-8")
                self.assertEqual(ParserSettings.load(path).date_only_deadline_time, value)
            for value in ("24:00", "23:59:00", "8:00", True, "nope"):
                with self.subTest(value=value), self.assertRaises(ValidationError):
                    ParserSettings(date_only_deadline_time=value)


class ExistingDefaultClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.service = TaskService(self.temp.name); self.service.initialize()
        self.task = self.service.add(course="【模拟】课程", title="【模拟】作业", deadline=Deadline.from_input("2026-12-02"), source=Notice("【模拟】12月2日前交", "2026-11-13"))

    def test_confirmation_required_before_existing_task_update(self):
        before = (self.service.store.root / "deadlines.json").read_bytes()
        with self.assertRaises(ValidationError): self.service.apply_default_deadline_time(self.task.id)
        self.assertEqual(before, (self.service.store.root / "deadlines.json").read_bytes())

    def test_existing_task_update_preserves_original_and_is_undoable(self):
        before = self.task
        changed = self.service.apply_default_deadline_time(before.id, confirmed=True, expected_version=before.updated_at)
        self.assertTrue(changed.changed)
        self.assertEqual(changed.task.deadline.time, "23:59")
        self.assertTrue(deadline_confirmed(changed.task))
        self.assertEqual(changed.task.source, before.source)
        self.assertEqual(changed.task.history[-1].action, "deadline_changed")
        self.assertIn("个人约定", changed.task.history[-1].changes["reason"])
        restored = self.service.recoverable_action(before.id, action="undo", confirmed=True)
        self.assertIsNone(restored.deadline.time)
        self.assertEqual(restored.history[-1].action, "undone")

    def test_repeated_application_does_not_create_new_history(self):
        first = self.service.apply_default_deadline_time(self.task.id, confirmed=True)
        again = self.service.apply_default_deadline_time(self.task.id, confirmed=True)
        self.assertFalse(again.changed)
        self.assertEqual(again.task, first.task)

    def test_explicit_time_completed_tasks_and_stale_views_protected(self):
        explicit = self.service.set_deadline(self.task.id, Deadline.from_input("2026-12-02T15:00")).task
        with self.assertRaises(ValidationError):
            self.service.apply_default_deadline_time(explicit.id, confirmed=True, expected_version=self.task.updated_at)
        self.assertFalse(self.service.apply_default_deadline_time(explicit.id, confirmed=True).changed)
        self.assertEqual(self.service.get(explicit.id).deadline.time, "15:00")
        self.service.complete(explicit.id)
        with self.assertRaises(ValidationError): self.service.apply_default_deadline_time(explicit.id, confirmed=True)

    def test_other_review_fields_preserved(self):
        with self.service.store.lock():
            reviewed = self.task.mutate({"review": TaskReview(True, ("course", "deadline"), ("课程需核对", "只有截止日期，尚无确定钟点"))}, "task_updated")
            self.service._save_tasks([reviewed], self.service.config())
        changed = self.service.apply_default_deadline_time(self.task.id, confirmed=True).task
        self.assertEqual(changed.review.uncertain_fields, ("course",))
        self.assertIn(default_clock_note("23:59"), changed.review.notes)

    def test_reminder_uses_default_2359_at_three_day_node(self):
        task = self.service.apply_default_deadline_time(self.task.id, confirmed=True).task
        now = datetime.fromisoformat("2026-11-29T23:59:00+08:00")
        plan = DeadlineReminderPlanner().plan([task], now, DEFAULT_REMINDERS)
        self.assertEqual([event.node for event in plan.events], ["lead:3"])
        self.assertTrue(plan.events[0].planned_at.endswith("23:59:00+08:00"))

    def test_uncertain_date_is_not_unblocked_by_a_clock(self):
        with self.service.store.lock():
            reviewed = self.task.mutate({"review": TaskReview(True, ("deadline",), ("截止日期可能变化，需核对",))}, "task_updated")
            self.service._save_tasks([reviewed], self.service.config())
        with self.assertRaises(ValidationError): self.service.apply_default_deadline_time(self.task.id, confirmed=True)
