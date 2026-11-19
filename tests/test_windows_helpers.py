"""Windows脚本的真实进程测试；只预览计划及运行终端包装器，不注册任务、不发桌面通知。"""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from course_assistant.service import TaskService

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "nt", "Windows helper integration only")
class WindowsHelperTests(unittest.TestCase):
    def test_unverifiable_interpreter_does_not_pass_preflight(self):
        fake = self.data.parent / "not_python.ps1"
        fake.write_text("$null = $args\n", encoding="ascii")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/configure_scheduler.ps1"),
                                 "-ProjectRoot", str(ROOT), "-DataDir", str(self.data), "-PythonExe", str(fake)],
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no native exit code", result.stderr)
        self.assertNotIn('"apply_requested"', result.stdout)

    def test_preview_resolves_python_launcher_when_python_not_on_path(self):
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        if not (windows / "py.exe").is_file(): self.skipTest("Python launcher not installed")
        env = {**os.environ, "PATH": str(windows / "System32") + ";" + str(windows)}
        result = subprocess.run([str(windows / "System32/WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(ROOT / "scripts/configure_scheduler.ps1"), "-ProjectRoot", str(ROOT), "-DataDir", str(self.data)],
                                env=env, capture_output=True, encoding="utf-8", timeout=30)
        if "DST host timezone" in result.stderr: self.skipTest("Host timezone has DST")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report, _ = json.JSONDecoder().raw_decode(result.stdout.strip())
        self.assertFalse(report["apply_requested"])
        self.assertIn("python.exe", report["arguments"].lower())

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="课程 提醒 "); self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        service = TaskService(self.data); service.initialize()
        service.update_preferences(enabled=True, check_time="19:45", channel="terminal")

    def run_script(self, name):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / name),
                                 "-ProjectRoot", str(ROOT), "-DataDir", str(self.data), "-PythonExe", sys.executable],
                                cwd=ROOT, capture_output=True, encoding="utf-8", timeout=30)
        if "DST host timezone" in result.stderr: self.skipTest("Host timezone has DST; helper deliberately requires manual review")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_scheduler_preview_reads_preferences_without_registering(self):
        text = self.run_script("configure_scheduler.ps1")
        report, _ = json.JSONDecoder().raw_decode(text.strip())
        self.assertFalse(report["apply_requested"])
        self.assertEqual(report["configured_time"], "19:45")
        self.assertTrue(report["start_when_available"])
        self.assertEqual(report["logon_type"], "Interactive")
        self.assertEqual(report["multiple_instances"], "IgnoreNew")
        self.assertIn(str(self.data), report["arguments"])
        self.assertIn("PREVIEW ONLY", text)

    def test_manual_wrapper_produces_complete_logs_without_claiming_scheduler_trigger(self):
        text = self.run_script("run_reminders.ps1")
        log = Path(text.strip())
        content = log.read_text(encoding="utf-8-sig")
        self.assertIn("wrapper_started", content)
        self.assertIn("wrapper_finished", content)
        self.assertIn('"exit_code":0', content)
        inner = list((self.data / "logs" / "reminder-runs").glob("*.jsonl"))
        records = [json.loads(line) for line in inner[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["trigger"], "manual")
        self.assertEqual(records[-1]["event"], "run_finished")
