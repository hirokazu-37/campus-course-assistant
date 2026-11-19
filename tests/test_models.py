from dataclasses import replace
import unittest

from course_assistant.errors import ValidationError
from course_assistant.models import Deadline, Notice, Submission, Task, parse_offset


class ModelTests(unittest.TestCase):
    def test_task_round_trip_and_unique_ids(self):
        first = Task.create(course="模拟课程", title="模拟作业", description="仅供测试，不是真实作业",
                            deadline=Deadline.from_input("2026-11-20T23:30"),
                            submission=Submission("模拟平台", "PDF"),
                            source=Notice("【模拟数据】通知\n保留原文。", "2026-11-17T10:00:00+08:00"))
        self.assertEqual(Task.from_dict(first.to_dict()), first)
        self.assertNotEqual(first.id, Task.create(course=first.course, title=first.title).id)
        self.assertEqual(first.history[0].action, "created")
        self.assertEqual(first.status, "pending")

    def test_unknown_deadline_does_not_invent_time(self):
        deadline = Deadline.from_input(None)
        self.assertIsNone(deadline.date)
        self.assertIsNone(deadline.time)
        self.assertEqual(deadline.precision, "unknown")
        self.assertTrue(deadline.needs_confirmation)

    def test_date_only_is_always_unconfirmed(self):
        deadline = Deadline.from_input("2026-11-20")
        self.assertEqual(deadline.precision, "date")
        self.assertIsNone(deadline.time)
        self.assertTrue(deadline.needs_confirmation)

    def test_minute_and_second_precision(self):
        minute = Deadline.from_input("2026-11-20T23:30")
        second = Deadline.from_input("2026-11-20T23:30:45", needs_confirmation=True)
        self.assertEqual((minute.time, minute.precision, minute.needs_confirmation), ("23:30", "minute", False))
        self.assertEqual((second.time, second.precision, second.needs_confirmation), ("23:30:45", "second", True))

    def test_invalid_deadline_inputs(self):
        for value in ("下周五", "2026-02-29", "2026-11-20T24:01", "2026-11-20T23:70", "2026-11-20T9:00",
                      "20261120", "2026-11-20T23:30+08:00", "2026-11-20T23:30:00.2", ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Deadline.from_input(value)

    def test_inconsistent_deadline_metadata_rejected(self):
        invalid = [dict(date="2026-11-20", precision="date", needs_confirmation=False),
                   dict(precision="unknown", needs_confirmation=False),
                   dict(date="2026-11-20", time="23:30", precision="date"),
                   dict(needs_confirmation="false"), dict(precision="week")]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                Deadline(**fields)

    def test_offsets_are_validated(self):
        self.assertEqual(parse_offset("+08:00").utcoffset(None).total_seconds(), 28800)
        for offset in ("Asia/Shanghai", "+14:01", "+25:00", "+08:99", "08:00", "+ 8:00", "+０８:00", None):
            with self.subTest(offset=offset), self.assertRaises(ValidationError):
                parse_offset(offset)

    def test_sent_at_requires_timezone(self):
        with self.assertRaises(ValidationError):
            Notice("【模拟】通知", "2026-11-17T12:00:00")
        self.assertIsNone(Notice().sent_at)

    def test_empty_names_and_invalid_fields_rejected(self):
        for fields in ({"course": " ", "title": "模拟作业"}, {"course": "模拟课程", "title": ""}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                Task.create(**fields)
        with self.assertRaises(ValidationError):
            Submission(method=123)

    def test_complete_is_idempotent_and_has_history(self):
        task = Task.create(course="模拟课程", title="模拟作业")
        completed = task.complete()
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.created_at, task.created_at)
        self.assertEqual(completed.updated_at, completed.completed_at)
        self.assertEqual(completed.history[-1].changes["status"], {"before": "pending", "after": "completed"})
        self.assertIs(completed.complete(), completed)

    def test_deadline_change_records_before_and_after(self):
        task = Task.create(course="模拟课程", title="模拟作业", deadline=Deadline.from_input("2026-11-20"))
        changed = task.set_deadline(Deadline.from_input("2026-11-22T22:00"))
        values = changed.history[-1].changes["deadline"]
        self.assertEqual(values["before"]["date"], "2026-11-20")
        self.assertEqual(values["after"]["time"], "22:00")
        self.assertEqual(changed.id, task.id)
        self.assertEqual(task.deadline.date, "2026-11-20")
        self.assertIs(changed.set_deadline(changed.deadline), changed)

    def test_invalid_persisted_task_rejected(self):
        task = Task.create(course="模拟课程", title="模拟作业")
        for field, value in (("id", "bad-id"), ("status", "doing"), ("completed_at", task.created_at),
                             ("history", []), ("created_at", "2026-11-17")):
            document = task.to_dict()
            document[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Task.from_dict(document)
        with self.assertRaises(ValidationError):
            replace(task, status="completed")

    def test_schema_rejects_missing_or_extra_fields(self):
        document = Task.create(course="模拟课程", title="模拟作业").to_dict()
        document["unexpected"] = "不应悄悄丢弃"
        with self.assertRaises(ValidationError):
            Task.from_dict(document)
        del document["unexpected"]
        del document["description"]
        with self.assertRaises(ValidationError):
            Task.from_dict(document)

    def test_history_timestamp_must_match_task(self):
        document = Task.create(course="模拟课程", title="模拟作业").to_dict()
        document["history"][0]["at"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaises(ValidationError):
            Task.from_dict(document)
