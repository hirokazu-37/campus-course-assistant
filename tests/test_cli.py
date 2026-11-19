import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def command(self, *arguments, expected=0):
        result = subprocess.run([sys.executable, "-m", "course_assistant", "--data-dir", str(self.root), *arguments],
                                cwd=PROJECT_ROOT, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, expected, msg=result.stdout + result.stderr)
        return result

    def add(self):
        return json.loads(self.command("add", "--course", "模拟课程", "--title", "模拟作业", "--due", "2026-11-20T23:00",
                                       "--notice", "【模拟数据】仅作自动测试").stdout)

    def test_end_to_end_init_add_query_show_complete_and_backup(self):
        initialized = json.loads(self.command("init").stdout)
        self.assertEqual(set(initialized["created_files"]), {"deadlines.json", "courses.json", "config.json"})
        task = self.add()
        self.assertEqual(len(json.loads(self.command("list", "--status", "pending", "--json").stdout)), 1)
        self.assertEqual(json.loads(self.command("show", task["id"]).stdout)["id"], task["id"])
        self.assertTrue(json.loads(self.command("complete", task["id"]).stdout)["changed"])
        self.assertFalse(json.loads(self.command("complete", task["id"]).stdout)["changed"])
        self.assertEqual(json.loads(self.command("list", "--status", "pending", "--json").stdout), [])
        self.assertEqual(len(json.loads(self.command("backup").stdout)["backups"]), 3)

    def test_invalid_input_and_unknown_id_have_nonzero_exit_without_traceback(self):
        result = self.command("add", "--course", "模拟课程", "--title", "模拟失败", "--due", "明天", expected=1)
        self.assertIn("错误", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(json.loads(self.command("list", "--json").stdout), [])
        self.command("show", "missing", expected=1)
        self.command("add", "--course", "模拟课程", expected=2)

    def test_notice_file_preserves_lines_and_sent_time(self):
        notice_file = self.root / "模拟通知.txt"
        raw = "【模拟数据】第一行\r\n第二行 📚\r\n"
        notice_file.write_bytes(raw.encode("utf-8-sig"))
        task = json.loads(self.command("add", "--course", "模拟课程", "--title", "模拟文件", "--notice-file", str(notice_file),
                                       "--sent-at", "2026-11-17T18:00:00+08:00").stdout)
        self.assertEqual(task["source"]["raw_notice"], raw)
        self.assertEqual(task["source"]["sent_at"], "2026-11-17T18:00:00+08:00")
        self.assertTrue(task["deadline"]["needs_confirmation"])

    def test_missing_notice_file_does_not_add_task(self):
        self.command("add", "--course", "模拟课程", "--title", "模拟失败", "--notice-file", str(self.root / "missing.txt"), expected=1)
        self.assertEqual(json.loads(self.command("list", "--json").stdout), [])

    def test_courses_and_manual_deadline_change(self):
        self.command("courses", "add", "--name", "模拟课程")
        self.assertEqual(len(json.loads(self.command("courses", "list").stdout)), 1)
        task = self.add()
        preview = self.command("set-deadline", task["id"], "--due", "2026-11-22").stdout
        self.assertIn("尚未保存", preview)
        self.assertNotIn('"changed": true', preview)
        self.assertEqual(json.loads(self.command("show", task["id"]).stdout)["deadline"]["date"], "2026-11-20")
        self.assertNotIn('"changed": true', self.command("set-deadline", task["id"], "--clear").stdout)

    def test_concurrent_cli_processes_preserve_both_tasks(self):
        processes = [subprocess.Popen([sys.executable, "-m", "course_assistant", "--data-dir", str(self.root),
                                      "add", "--course", "模拟并发课程", "--title", f"模拟进程{i}"], cwd=PROJECT_ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8") for i in range(3)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(len(json.loads(self.command("list", "--json").stdout)), 3)

    def test_plain_list_and_help(self):
        self.assertIn("没有符合条件", self.command("list").stdout)
        task = self.add()
        self.assertIn(task["id"], self.command("list").stdout)
        self.assertIn("AI 解析", self.command("--help").stdout)
