import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from course_assistant.errors import StorageError
from course_assistant.service import TaskService
from course_assistant.storage import JsonStore


class StorageTests(unittest.TestCase):
    def test_valid_windows_utf8_bom_json_is_accepted_without_reset(self):
        task = self.service.add(course="模拟课程", title="模拟BOM兼容")
        path = self.root / "deadlines.json"
        payload = b"\xef\xbb\xbf" + path.read_bytes()
        path.write_bytes(payload)
        self.assertEqual(self.service.initialize(), [])
        self.assertEqual(self.service.get(task.id), task)
        self.assertEqual(path.read_bytes(), payload)

    def test_transient_windows_permission_error_during_lock_handoff_is_retried(self):
        actual_open = os.open
        attempts = []
        def transient(path, *args, **kwargs):
            if Path(path).name == ".write.lock":
                attempts.append(path)
                if len(attempts) == 1: raise PermissionError("模拟 Windows 删除交接期间暂时拒绝访问")
            return actual_open(path, *args, **kwargs)
        with patch("course_assistant.storage.os.open", side_effect=transient):
            with self.store.lock(): self.assertTrue((self.root / ".write.lock").exists())
        self.assertEqual(len(attempts), 2)
        self.assertFalse((self.root / ".write.lock").exists())

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = TaskService(self.root)
        self.service.initialize()
        self.store = self.service.store

    def test_backup_contains_exact_previous_bytes(self):
        previous = (self.root / "deadlines.json").read_bytes()
        self.service.add(course="模拟课程", title="模拟作业")
        paths = list((self.root / "backups" / "deadlines").glob("*.json"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].read_bytes(), previous)

    def test_retention_is_per_file(self):
        with self.store.lock():
            for index in range(5):
                self.store.write("deadlines.json", {"schema_version": 1, "tasks": [], "test_index": index}, keep=2)
        paths = sorted((self.root / "backups" / "deadlines").glob("*.json"))
        self.assertEqual(len(paths), 2)
        self.assertEqual([json.loads(path.read_text(encoding="utf-8"))["test_index"] for path in paths], [2, 3])

    def test_replace_failure_preserves_original_and_cleans_temp(self):
        path = self.root / "deadlines.json"
        before = path.read_bytes()
        real_replace = os.replace
        def failing_replace(source, destination):
            self.assertEqual(Path(source).parent, Path(destination).parent)
            if Path(destination) == path:
                raise PermissionError("模拟文件占用")
            return real_replace(source, destination)
        with patch("course_assistant.storage.os.replace", side_effect=failing_replace):
            with self.assertRaises(StorageError):
                self.service.add(course="模拟课程", title="模拟失败")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.rglob("*.tmp")), [])
        self.assertFalse((self.root / ".write.lock").exists())

    def test_backup_failure_aborts_before_replace(self):
        before = (self.root / "deadlines.json").read_bytes()
        with patch.object(self.store, "_backup", side_effect=OSError("模拟备份磁盘不可用")):
            with self.assertRaises(StorageError):
                self.service.add(course="模拟课程", title="模拟失败")
        self.assertEqual((self.root / "deadlines.json").read_bytes(), before)

    def test_fsync_failure_preserves_original(self):
        before = (self.root / "deadlines.json").read_bytes()
        # 锁已成功创建后，模拟临时文件 fsync 失败。
        with self.store.lock():
            with patch("course_assistant.storage.os.fsync", side_effect=OSError("模拟磁盘写入失败")):
                with self.assertRaises(StorageError):
                    self.store.write("deadlines.json", {"schema_version": 1, "tasks": []})
        self.assertEqual((self.root / "deadlines.json").read_bytes(), before)
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_malformed_json_duplicate_keys_nan_and_invalid_encoding_rejected(self):
        path = self.root / "deadlines.json"
        for payload in (b'{"tasks":', b'{"tasks": [], "tasks": []}', b'{"value": NaN}', b'\xff', b'[]'):
            path.write_bytes(payload)
            with self.subTest(payload=payload), self.assertRaises(StorageError):
                self.store.read("deadlines.json")
            with self.assertRaises(StorageError):
                with self.store.lock():
                    self.store.write("deadlines.json", {"schema_version": 1, "tasks": []})
            self.assertEqual(path.read_bytes(), payload)

    def test_lock_timeout_does_not_remove_another_lock(self):
        other = JsonStore(self.root, lock_timeout=0.02)
        with self.store.lock():
            before = (self.root / ".write.lock").read_bytes()
            with self.assertRaises(StorageError):
                with other.lock():
                    self.fail("不应取得已占用的锁")
            self.assertEqual((self.root / ".write.lock").read_bytes(), before)
        self.assertFalse((self.root / ".write.lock").exists())

    def test_lock_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.store.lock():
                raise RuntimeError("模拟业务失败")
        self.assertFalse((self.root / ".write.lock").exists())

    def test_audit_failure_warns_without_claiming_save_failed(self):
        task = self.service.add(course="模拟课程", title="模拟作业")
        original_open = Path.open
        def fail_log(path, *args, **kwargs):
            if path.suffix == ".jsonl":
                raise PermissionError("模拟日志目录只读")
            return original_open(path, *args, **kwargs)
        with patch("pathlib.Path.open", new=fail_log), self.assertWarnsRegex(RuntimeWarning, "操作已保存"):
            result = self.service.complete(task.id)
        self.assertTrue(result.changed)
        self.assertEqual(self.service.get(task.id).status, "completed")

    def test_retention_failure_is_a_warning_after_success(self):
        with patch.object(self.store, "_prune", side_effect=PermissionError("模拟旧备份被占用")):
            with self.assertWarnsRegex(RuntimeWarning, "数据已保存"):
                task = self.service.add(course="模拟课程", title="模拟作业")
        self.assertEqual(self.service.get(task.id), task)

    def test_data_path_allowlist(self):
        with self.assertRaises(StorageError):
            self.store.read("../outside.json")
