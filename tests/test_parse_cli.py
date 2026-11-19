from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from course_assistant.cli import main
from course_assistant.errors import ModelError
from course_assistant.models import Task
from course_assistant.service import TaskService
from tests.parser_fixtures import RAW, SENT_AT, FakeClient, response


class ParseCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        service = TaskService(self.root); service.initialize()
        service.add_course("中级微观经济学")  # 模拟用户先登记课程，全程无预设简称映射。

    def run_cli(self, *args, output=None, answers=(), interactive=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        fake = FakeClient(output if output is not None else response())
        with redirect_stdout(stdout), redirect_stderr(stderr), patch("course_assistant.llm.ApiSettings.from_env"), \
             patch("course_assistant.llm.StructuredClient", return_value=fake), patch("builtins.input", side_effect=answers), \
             patch("course_assistant.cli.sys.stdin.isatty", return_value=interactive), patch("course_assistant.cli.secrets.token_hex", return_value="abc123"):
            code = main(["--data-dir", str(self.root), "parse", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_preview_has_no_tasks_and_no_confirmation(self):
        code, text, error = self.run_cli("--text", RAW, "--sent-at", SENT_AT, "--preview-only")
        self.assertEqual(code, 0, error)
        self.assertIn("解析预览", text)
        self.assertEqual(TaskService(self.root).list(), [])

    def test_cancel_does_not_write(self):
        code, text, error = self.run_cli("--text", RAW, "--sent-at", SENT_AT, answers=["取消"])
        self.assertEqual(code, 0, error)
        self.assertIn("已取消", text)
        self.assertEqual(TaskService(self.root).list(), [])

    def test_confirm_is_required_after_display_and_imports_batch(self):
        output = response(); output["tasks"].append(response()["tasks"][0])
        code, text, error = self.run_cli("--text", RAW, "--sent-at", SENT_AT, output=output, answers=["确认导入 abc123"])
        self.assertEqual(code, 0, error)
        self.assertEqual(len(TaskService(self.root).list()), 1)
        self.assertIn('"duplicates": 1', text)
        self.assertLess(text.index("解析预览"), text.index('"imported"'))

    def test_uncertain_fields_need_explicit_retention_phrase(self):
        output = response(format_requirements=None, format_evidence="")
        code, text, error = self.run_cli("--text", RAW, "--sent-at", SENT_AT, output=output, answers=["确认导入并保留待确认 abc123"])
        self.assertEqual(code, 0, error)
        self.assertTrue(TaskService(self.root).list()[0].review.needs_confirmation)
        self.assertIn("仍有待确认字段", text)

    def test_noninteractive_cannot_confirm(self):
        code, text, error = self.run_cli("--text", RAW, "--sent-at", SENT_AT, interactive=False)
        self.assertEqual(code, 0, error)
        self.assertIn("非交互", text)
        self.assertEqual(TaskService(self.root).list(), [])

    def test_reads_inbox_by_default(self):
        (self.root / "inbox.txt").write_text(RAW, encoding="utf-8")
        with chdir(self.root):
            code, text, error = self.run_cli("--sent-at", SENT_AT, "--preview-only")
        self.assertEqual(code, 0, error)
        self.assertIn("第3次作业", text)

    def test_paste_input_and_custom_file(self):
        code, text, error = self.run_cli("--paste", "--sent-at", SENT_AT, "--preview-only", answers=[RAW, "::end"])
        self.assertEqual(code, 0, error)
        path = self.root / "通知.txt"; path.write_text(RAW, encoding="utf-8-sig")
        code, text, error = self.run_cli("--file", str(path), "--sent-at", SENT_AT, "--preview-only")
        self.assertEqual(code, 0, error)

    def test_no_task_and_model_error_never_write(self):
        code, text, error = self.run_cli("--text", "【模拟】大家好", output={"classification": "non_task", "tasks": [], "warnings": []})
        self.assertEqual(code, 0, error)
        self.assertIn("未识别到课程任务", text)
        with patch("course_assistant.parsing.AINotificationParser.analyze", side_effect=ModelError("模拟模型异常")):
            code, text, error = self.run_cli("--text", RAW)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", error)
        self.assertEqual(TaskService(self.root).list(), [])

    def test_missing_key_is_reported_without_fake_model_fallback(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--data-dir", str(self.root), "parse", "--text", RAW, "--preview-only"])
        self.assertEqual(code, 1)
        self.assertIn("OPENAI_API_KEY", stderr.getvalue())
        self.assertEqual(TaskService(self.root).list(), [])

    def test_config_can_disable_model_calls(self):
        service = TaskService(self.root); service.initialize()
        with service.store.lock():
            config = service.config(); config["ai_enabled"] = False; service.store.write("config.json", config)
        code, text, error = self.run_cli("--text", RAW)
        self.assertEqual(code, 1)
        self.assertIn("ai_enabled", error)
