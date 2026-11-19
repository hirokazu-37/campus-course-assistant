from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from course_assistant.errors import StorageError, TaskNotFoundError, ValidationError
from course_assistant.models import Deadline, Notice
from course_assistant.service import TaskService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = TaskService(self.root)
        self.service.initialize()

    def add(self, title="模拟作业", due=None, course="模拟课程"):
        return self.service.add(course=course, title=title, deadline=Deadline.from_input(due), source=Notice("【模拟数据】仅用于测试"))

    def test_initialize_is_idempotent_and_preserves_data(self):
        task = self.add()
        before = (self.root / "deadlines.json").read_bytes()
        self.assertEqual(self.service.initialize(), [])
        self.assertEqual((self.root / "deadlines.json").read_bytes(), before)
        self.assertEqual(self.service.get(task.id), task)

    def test_reopen_persists_unicode_and_raw_notice(self):
        task = self.add("模拟经济金融任务 📚")
        reopened = TaskService(self.root)
        reopened.initialize()
        self.assertEqual(reopened.get(task.id), task)
        self.assertIn("模拟经济金融", (self.root / "deadlines.json").read_text(encoding="utf-8"))

    def test_deadline_order_and_unknown_last_both_directions(self):
        unknown = self.add("模拟未知")
        late = self.add("模拟晚", "2026-11-22T23:00")
        date_only = self.add("模拟日期", "2026-11-20")
        early = self.add("模拟早", "2026-11-20T12:00")
        self.assertEqual([t.id for t in self.service.list()], [date_only.id, early.id, late.id, unknown.id])
        self.assertEqual([t.id for t in self.service.list(descending=True)], [late.id, early.id, date_only.id, unknown.id])
        self.assertIsNone(self.service.get(date_only.id).deadline.time)

    def test_deadlines_sort_by_actual_offset(self):
        first = self.service.add(course="模拟课程", title="UTC早", deadline=Deadline.from_input("2026-11-20T09:00", "+09:00"))
        second = self.service.add(course="模拟课程", title="UTC晚", deadline=Deadline.from_input("2026-11-20T08:30", "+08:00"))
        self.assertEqual([t.id for t in self.service.list()], [first.id, second.id])

    def test_query_filters_status_course_and_substring(self):
        task = self.add("模拟报告", course="模拟金融学")
        other = self.add("模拟习题", course="模拟统计学")
        self.service.complete(other.id)
        self.assertEqual([t.id for t in self.service.list(status="pending")], [task.id])
        self.assertEqual([t.id for t in self.service.list(course="模拟统计学", query="习题")], [other.id])
        self.assertEqual(self.service.list(query="不匹配"), [])

    def test_sort_by_course_and_creation(self):
        first = self.add("模拟1", course="B")
        second = self.add("模拟2", course="A")
        self.assertEqual([t.id for t in self.service.list(sort="course")], [second.id, first.id])
        self.assertEqual([t.id for t in self.service.list(sort="created")], [first.id, second.id])

    def test_completion_persisted_and_repeat_is_noop(self):
        task = self.add()
        self.assertTrue(self.service.complete(task.id).changed)
        before = (self.root / "deadlines.json").read_bytes()
        backups = list((self.root / "backups" / "deadlines").glob("*.json"))
        self.assertFalse(self.service.complete(task.id).changed)
        self.assertEqual((self.root / "deadlines.json").read_bytes(), before)
        self.assertEqual(list((self.root / "backups" / "deadlines").glob("*.json")), backups)
        self.assertEqual(TaskService(self.root).get(task.id).status, "completed")

    def test_manual_deadline_update_can_clear_without_changing_status(self):
        task = self.add(due="2026-11-20")
        result = self.service.set_deadline(task.id, Deadline.from_input("2026-11-22T23:00"))
        self.assertEqual(result.task.history[-1].action, "deadline_changed")
        result = self.service.set_deadline(task.id, Deadline())
        self.assertIsNone(result.task.deadline.date)
        self.assertTrue(result.task.deadline.needs_confirmation)
        self.assertEqual(result.task.status, "pending")

    def test_unknown_task_does_not_write(self):
        before = (self.root / "deadlines.json").read_bytes()
        for operation in (self.service.get, self.service.complete):
            with self.subTest(operation=operation.__name__), self.assertRaises(TaskNotFoundError):
                operation("missing")
        self.assertEqual((self.root / "deadlines.json").read_bytes(), before)

    def test_invalid_query_rejected(self):
        for kwargs in ({"status": "doing"}, {"sort": "title"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.service.list(**kwargs)

    def test_course_directory_separate_from_tasks(self):
        course = self.service.add_course(" 模拟课程 ")
        self.assertEqual(self.service.courses(), [course])
        self.assertEqual(self.service.list(), [])
        with self.assertRaises(ValidationError):
            self.service.add_course("模拟课程")
        # 不做隐式跨文件写入；目录可选，人工任务无需先注册课程。
        self.add(course="模拟未注册课程")
        self.assertEqual(len(self.service.courses()), 1)

    def test_manual_snapshot_includes_three_valid_files(self):
        paths = self.service.backup()
        self.assertEqual(len(paths), 3)
        for path in paths:
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_semantic_corruption_is_not_overwritten(self):
        document = {"schema_version": 1, "tasks": [{"id": "invalid"}]}
        path = self.root / "deadlines.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(StorageError):
            self.service.initialize()
        with self.assertRaises(StorageError):
            self.add()
        self.assertEqual(path.read_bytes(), before)

    def test_duplicate_task_ids_rejected(self):
        task = self.add()
        path = self.root / "deadlines.json"
        path.write_text(json.dumps({"schema_version": 1, "tasks": [task.to_dict(), task.to_dict()]}), encoding="utf-8")
        with self.assertRaises(StorageError):
            self.service.list()

    def test_unknown_schema_version_fails_without_resetting(self):
        path = self.root / "config.json"
        path.write_text('{"schema_version": 2}', encoding="utf-8")
        with self.assertRaises(StorageError):
            self.service.initialize()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"schema_version": 2})

    def test_reminders_cannot_be_enabled_and_ai_requires_boolean(self):
        for key, value in (("ai_enabled", "true"), ("reminders_enabled", "true")):
            config = self.service.config()
            config[key] = value
            path = self.root / "config.json"
            before = path.read_bytes()
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.subTest(key=key), self.assertRaises(StorageError):
                self.service.initialize()
            path.write_bytes(before)

    def test_logs_are_json_lines_without_notice_content(self):
        task = self.add()
        self.service.complete(task.id)
        content = "".join(path.read_text(encoding="utf-8") for path in (self.root / "logs").glob("*.jsonl"))
        events = [json.loads(line) for line in content.splitlines()]
        self.assertIn("task_added", [event["action"] for event in events])
        self.assertIn("task_completed", [event["action"] for event in events])
        self.assertNotIn("仅用于测试", content)

    def test_concurrent_writers_do_not_lose_tasks(self):
        def add_one(index):
            return TaskService(self.root).add(course="模拟并发课程", title=f"模拟并发任务{index}").id
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(add_one, range(12)))
        self.assertEqual({task.id for task in self.service.list()}, set(ids))
        self.assertFalse((self.root / ".write.lock").exists())
