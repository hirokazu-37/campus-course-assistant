"""桌面启动器测试：临时台账、真实本地服务；不调用模型，不注册系统任务。"""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import urllib.request

from course_assistant import desktop
from course_assistant.errors import StorageError
from course_assistant.service import TaskService


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"

    def test_source_and_frozen_roots_do_not_depend_on_working_directory(self):
        self.assertEqual(desktop.application_root(), Path(__file__).resolve().parent.parent)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(self.root / "assets"), create=True), patch.object(sys, "executable", str(self.root / "app.exe")):
            self.assertEqual(desktop.application_root(), self.root)
            self.assertEqual(desktop.resource_root(), self.root / "assets")

    def test_new_runtime_initializes_json_and_only_copies_missing_settings(self):
        settings = desktop.prepare_runtime(self.root, self.data)
        self.assertTrue(settings.is_file())
        self.assertEqual(TaskService(self.data).list(), [])
        original = settings.read_bytes()
        desktop.prepare_runtime(self.root, self.data)
        self.assertEqual(settings.read_bytes(), original)

    def test_existing_courses_tasks_and_settings_are_preserved(self):
        service = TaskService(self.data); service.initialize()
        task = service.add(course="【模拟】课程", title="【模拟】任务")
        service.add_course("【模拟】课程")
        folder = self.root / "settings"; folder.mkdir()
        settings = folder / "parsing.json"
        settings.write_text('{"schema_version":1,"timezone":"Asia/Shanghai","semester_week1_monday":"2026-09-07"}', encoding="utf-8")
        before = {p: p.read_bytes() for p in (self.data / "deadlines.json", self.data / "courses.json", settings)}
        desktop.prepare_runtime(self.root, self.data)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(service.get(task.id).title, "【模拟】任务")

    def test_corrupt_data_stops_before_copying_settings(self):
        TaskService(self.data).initialize()
        path = self.data / "deadlines.json"; path.write_bytes(b'{"broken":')
        with self.assertRaises(StorageError):
            desktop.prepare_runtime(self.root, self.data)
        self.assertEqual(path.read_bytes(), b'{"broken":')
        self.assertFalse((self.root / "settings").exists())

    def test_server_command_preserves_paths_as_separate_arguments(self):
        command = desktop.server_command(self.root / "项目 空格", self.data, 23000, "desktop-test", self.root / "run.log")
        self.assertEqual(command[command.index("--root") + 1], str(self.root / "项目 空格"))
        self.assertIn("--stop-file", command)
        self.assertEqual(command[0], sys.executable)
        with patch.object(sys, "frozen", True, create=True):
            frozen = desktop.server_command(self.root, self.data, 23000, "desktop-test", self.root / "run.log")
        self.assertEqual(frozen[1], "--serve")

    def test_port_is_released_for_the_owned_server(self):
        port = desktop.choose_port()
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))

    def test_spawn_failure_closes_log_handle(self):
        log = desktop.DesktopLog(self.data)
        server = desktop.LocalServer(self.root, self.data, log)
        with patch("course_assistant.desktop.subprocess.Popen", side_effect=OSError("模拟启动失败")), self.assertRaises(OSError):
            server.start()
        self.assertIsNone(server.output)
        server.stop()  # 重复退出安全。

    def test_failed_backend_is_rejected_and_only_owned_resources_are_closed(self):
        log = desktop.DesktopLog(self.data)
        server = desktop.LocalServer(self.root, self.data, log)
        process = MagicMock(); process.poll.return_value = 1; process.pid = 1234
        with patch("course_assistant.desktop.subprocess.Popen", return_value=process), patch("course_assistant.desktop.WindowsJob") as job, self.assertRaises(RuntimeError):
            server.start()
        job.return_value.close.assert_called_once()
        self.assertIsNone(server.process)

    def test_gui_is_private_local_and_does_not_log_key(self):
        fake = SimpleNamespace(create_window=MagicMock(), start=MagicMock())
        secret = "desktop-test-secret-must-not-persist"
        with patch.dict(sys.modules, {"webview": fake}), patch.dict(os.environ, {"OPENAI_API_KEY": secret}), patch("course_assistant.desktop.LocalServer") as server:
            server.return_value.url = "http://127.0.0.1:23001/desktop-test/"
            self.assertEqual(desktop.main(["--root", str(self.root), "--data-dir", str(self.data)]), 0)
        self.assertEqual(fake.create_window.call_args.args[1], "http://127.0.0.1:23001/desktop-test/")
        self.assertTrue(fake.start.call_args.kwargs["private_mode"])
        self.assertFalse(fake.start.call_args.kwargs["debug"])
        server.return_value.stop.assert_called_once()
        for path in self.data.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())

    def test_smoke_mode_requires_explicit_test_data_dir(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            desktop.main(["--smoke-test"])

    def test_local_backend_starts_and_exits_gracefully_without_a_window(self):
        desktop.prepare_runtime(self.root, self.data)
        log = desktop.DesktopLog(self.data)
        server = desktop.LocalServer(self.root, self.data, log)
        try:
            server.start(timeout=25)
            self.assertIsNone(server.process.poll())
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(server.url + "_stcore/health", timeout=3) as result:
                self.assertEqual(result.read(), b"ok")
        finally:
            server.stop()
        rows = [json.loads(row) for row in log.path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["event"] == "server_stopped" and row["graceful"] for row in rows))
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", server.port)), 0)

    def test_busy_close_guard_only_matches_its_active_worker_locks(self):
        log = desktop.DesktopLog(self.data)
        server = desktop.LocalServer(self.root, self.data, log)
        server.process = MagicMock(); server.process.poll.return_value = None
        server.worker_pid = 12345
        path = self.data / "cache" / "parse" / ".write.lock"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
        self.assertTrue(server.busy())
        with patch("course_assistant.desktop.ctypes.windll.user32.MessageBoxW"):
            self.assertFalse(desktop.allow_close(server))
        path.write_text(json.dumps({"pid": 99999}), encoding="utf-8")
        self.assertFalse(server.busy())
        self.assertTrue(desktop.allow_close(server))
        path.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
        server.process.poll.return_value = 0
        self.assertFalse(server.busy())
