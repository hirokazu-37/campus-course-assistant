"""通过实际 CLI 编排验证人工确认、缓存和恢复；API 全部为模拟响应。"""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from course_assistant.cli import main
from course_assistant.models import Deadline, Notice, Submission
from course_assistant.service import TaskService
from tests.parser_fixtures import FakeClient, deadline, response
from tests.test_relations import FOLLOWUP, ORIGINAL, SENT, relation


class Round3CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = TaskService(self.root); self.service.initialize()
        self.old = self.service.add(course="微观经济学", title="微观经济学作业", description="课后题1至5",
                                    deadline=Deadline.from_input("2026-11-25"), submission=Submission("平台", "PDF"),
                                    source=Notice(ORIGINAL, "2026-11-17T18:00:00+08:00"))
        parsed = response(course_evidence="微观", title="上次的微观作业", description=FOLLOWUP, task_evidence="上次的微观作业",
                          deadline=deadline("absolute", "11月27日"), submission_method=None, submission_evidence="", format_requirements=None, format_evidence="")
        self.parse_client, self.relation_client = FakeClient(parsed), FakeClient(relation([self.old.id]))

    def command(self, *args, answers=(), interactive=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        def client_factory(*positional, **kwargs):
            return self.relation_client if kwargs.get("schema_name") == "notice_relation" else self.parse_client
        with redirect_stdout(stdout), redirect_stderr(stderr), patch("course_assistant.llm.ApiSettings.from_env"), \
             patch("course_assistant.llm.StructuredClient", side_effect=client_factory), patch("builtins.input", side_effect=answers), \
             patch("course_assistant.cli.sys.stdin.isatty", return_value=interactive), patch("course_assistant.cli.secrets.token_hex", return_value="abc123"):
            code = main(["--data-dir", str(self.root), *args])
        self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
        return stdout.getvalue()

    def parse(self, **kwargs):
        return self.command("parse", "--text", FOLLOWUP, "--sent-at", SENT, **kwargs)

    def test_cli_shows_matching_basis_and_before_after_before_confirmation(self):
        output = self.parse(answers=["取消"])
        self.assertIn("已有任务的修改通知", output)
        self.assertIn("2026-11-25", output)
        self.assertIn("2026-11-27", output)
        self.assertIn("延期", output)
        self.assertIn("已取消", output)
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")

    def test_cli_confirm_then_repeat_has_zero_more_model_calls(self):
        self.parse(answers=["确认更新并保留待确认 abc123"])
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-27")
        count = (len(self.parse_client.calls), len(self.relation_client.calls))
        output = self.parse()
        self.assertIn("已处理", output)
        self.assertEqual((len(self.parse_client.calls), len(self.relation_client.calls)), count)

    def test_cli_cancel_and_preview_again_reuses_both_caches(self):
        self.parse(answers=["取消"])
        self.command("parse", "--text", FOLLOWUP, "--sent-at", SENT, "--preview-only")
        self.assertEqual((len(self.parse_client.calls), len(self.relation_client.calls)), (1, 1))
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")

    def test_noninteractive_update_never_applies(self):
        self.parse(interactive=False)
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")

    def test_cli_ambiguous_can_choose_target_and_known_due(self):
        other = self.service.add(course="微观经济学", title="第2次作业")
        self.relation_client.output = relation([self.old.id, other.id], kind="uncertain")
        output = self.parse()
        self.assertIn("无法确定，需要人工判断", output)
        self.command("parse", "--text", FOLLOWUP, "--sent-at", SENT, "--target", self.old.id, "--due", "2026-11-27",
                     answers=["确认更新并保留待确认 abc123"])
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-27")
        self.assertIsNone(self.service.get(other.id).deadline.date)

    def test_cli_undo_last_modification_keeps_old_sources(self):
        self.parse(answers=["确认更新并保留待确认 abc123"])
        output = self.command("undo", answers=["确认撤销 abc123"])
        self.assertIn("撤销最近修改", output)
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")
        self.assertEqual(self.service.get(self.old.id).history[-1].action, "undone")

    def test_cli_delete_reject_and_confirm_then_restore(self):
        self.command("delete", self.old.id, answers=["取消"])
        self.assertIsNone(self.service.get(self.old.id).deleted_at)
        self.command("delete", self.old.id, answers=["确认删除 abc123"])
        self.assertEqual(self.service.list(), [])
        self.command("undo", self.old.id, answers=["确认撤销 abc123"])
        self.assertEqual(len(self.service.list()), 1)

    def test_manual_deadline_update_requires_confirmation_and_is_undoable(self):
        self.command("set-deadline", self.old.id, "--due", "2026-11-27", answers=["取消"])
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")
        self.command("set-deadline", self.old.id, "--due", "2026-11-27", answers=["确认修改截止时间 abc123"])
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-27")
        self.command("undo", self.old.id, answers=["确认撤销 abc123"])
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")
