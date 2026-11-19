"""完整流程联调：真实HTTP客户端对本机模拟服务，模拟确认/时钟/通知，绝非真实使用证据。"""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from course_assistant.cli import main
from course_assistant.llm import ApiSettings
from course_assistant.notifications import Delivery
from course_assistant.reminders import ReminderRunner
from course_assistant.service import TaskService
from tests.parser_fixtures import RAW, SENT_AT, deadline, response

FOLLOWUP = "【模拟联调通知】中微第3次作业延期到2026年11月22日23:30，其他要求不变。"
FOLLOWUP_SENT = "2026-11-18T18:00:00+08:00"


@contextmanager
def integration_api():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.loads(body["messages"][1]["content"])
            requests.append(payload)
            if "untrusted_time_input" in payload:
                evidence = payload["untrusted_time_input"] or payload["untrusted_notice_headers"][0]
                output = {"date": "2026-11-13", "time": None, "evidence": evidence, "uncertain": False, "reason": "【模拟】明确发送日期，无钟点"}
            elif "new_task" in payload:
                output = {"kind": "possible_update", "target_ids": [payload["candidates"][0]["id"]],
                          "reason": "【模拟响应】同课程同一次作业的日期修改", "evidence": "第3次作业延期",
                          "changes": [{"field": "deadline", "evidence": "延期到2026年11月22日23:30"}]}
            elif payload["untrusted_notice"] == FOLLOWUP:
                output = response(task_evidence="第3次作业延期", deadline=deadline(expression="2026年11月22日23:30", date="2026-11-22", time="23:30", time_expression="23:30"),
                                  submission_method=None, submission_evidence="", format_requirements=None, format_evidence="")
            elif payload["untrusted_notice"].startswith("【模拟年份通知】"):
                output = response(deadline=deadline("absolute", "12月2日前", uncertain=True),
                                  notes=["截止日期“12月2日前”缺少年份，已标记为不确定。"])
                output["warnings"] = ["截止日期缺少年份，已标记为不确定。"]
            else: output = response()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output, ensure_ascii=False)}}]}, ensure_ascii=False).encode())
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally: server.shutdown(); server.server_close(); thread.join(timeout=3)


class EndToEndTests(unittest.TestCase):
    def test_paste_parse_confirm_save_postpone_catchup_complete_both_modes(self):
        for mode in ("json_schema", "json_object"):
            with self.subTest(mode=mode), TemporaryDirectory() as folder, integration_api() as (url, requests):
                root = Path(folder)
                settings = ApiSettings("unit-test-not-a-real-key", "mock", url, response_mode=mode)
                def command(*args, answers=()):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err), patch("course_assistant.llm.ApiSettings.from_env", return_value=settings), \
                         patch("course_assistant.cli.sys.stdin.isatty", return_value=True), patch("course_assistant.cli.secrets.token_hex", return_value="abcdef"), \
                         patch("builtins.input", side_effect=answers):
                        code = main(["--data-dir", str(root), *args])
                    self.assertEqual(code, 0, out.getvalue() + err.getvalue())
                    return out.getvalue()
                command("preferences", "set", "--enable", "--channel", "terminal")
                command("courses", "add", "--name", "中级微观经济学")
                command("parse", "--paste", "--sent-at", SENT_AT, answers=[RAW, "::end", "确认导入 abcdef"])
                service = TaskService(root)
                tasks = service.list(); self.assertEqual(len(tasks), 1)
                task_id = tasks[0].id
                saved = json.loads((root / "deadlines.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["tasks"][0]["deadline"]["date"], "2026-11-20")
                self.assertEqual(len(requests), 1)
                self.assertIn("已处理", command("parse", "--text", RAW, "--sent-at", SENT_AT))
                self.assertEqual(len(requests), 1)
                command("parse", "--text", FOLLOWUP, "--sent-at", FOLLOWUP_SENT, answers=["取消"])
                self.assertEqual(service.get(task_id).deadline.date, "2026-11-20")
                self.assertEqual(len(requests), 3)
                command("parse", "--text", FOLLOWUP, "--sent-at", FOLLOWUP_SENT, answers=["确认更新并保留待确认 abcdef"])
                self.assertEqual(len(requests), 3)  # 取消后的解析与关系结果全部命中缓存。
                updated = service.get(task_id)
                self.assertEqual(updated.deadline.date, "2026-11-22")
                self.assertEqual(updated.submission.method, "教学平台")
                self.assertEqual(updated.source.raw_notice, RAW)
                self.assertEqual(updated.history[-1].changes["notice"]["raw_notice"], FOLLOWUP)
                self.assertEqual(len(service.list()), 1)
                deliveries = []
                class FakeNotifier:
                    def send(self, title, message):
                        deliveries.append(message); return Delivery("terminal", fallback_reason="模拟通知")
                runner = ReminderRunner(service, FakeNotifier())
                with redirect_stdout(io.StringIO()):
                    reminder = runner.check(now=datetime.fromisoformat("2026-11-19T23:30:00+08:00"))
                self.assertEqual(reminder["events"][0]["node"], "lead:3")
                command("complete", task_id)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.check(now=datetime.fromisoformat("2026-11-21T23:30:00+08:00"))["count"], 0)
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(service.get(task_id).status, "completed")
                self.assertEqual(len(requests), 3)
                # 模拟磁盘文件损坏后，从本次明确创建的备份恢复，保留处理凭据和完成状态。
                backups = service.backup()
                snapshot = next(path for path in backups if path.parent.name == "deadlines")
                ledger = root / "deadlines.json"
                ledger.write_bytes(b'{"broken":')
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--data-dir", str(root), "init"]), 1)
                self.assertEqual(ledger.read_bytes(), b'{"broken":')
                ledger.write_bytes(snapshot.read_bytes())
                service.initialize()
                self.assertEqual(service.get(task_id).status, "completed")
                self.assertEqual(len(service.document()["processed_batches"]), 2)

    def test_corrupt_ledger_stops_before_any_http_request(self):
        with TemporaryDirectory() as folder, integration_api() as (url, requests):
            service = TaskService(folder); service.initialize()
            path = Path(folder) / "deadlines.json"; path.write_bytes(b'{"tasks":')
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch("course_assistant.llm.ApiSettings.from_env", return_value=ApiSettings("fake", "mock", url)):
                code = main(["--data-dir", folder, "parse", "--text", RAW, "--sent-at", SENT_AT, "--preview-only"])
            self.assertEqual(code, 1)
            self.assertEqual(path.read_bytes(), b'{"tasks":')
            self.assertEqual(requests, [])
