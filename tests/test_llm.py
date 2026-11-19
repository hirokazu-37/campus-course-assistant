from contextlib import contextmanager
import io
import json
import os
import socket
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
import urllib.error

from course_assistant.errors import ModelError
from course_assistant.llm import ApiSettings, StructuredClient
from course_assistant.parsing import AINotificationParser
from course_assistant.models import Notice
from tests.parser_fixtures import RAW, SENT_AT, context, response, settings

FAKE_KEY = "not-a-real-key-unit-test-only"


@contextmanager
def mock_api(payload=None, status=200, location=None):
    """本地模拟 HTTP 服务，不是第三方真实模型。"""
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if location: self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(json.dumps(payload if payload is not None else {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response(), ensure_ascii=False)}}]}, ensure_ascii=False).encode("utf-8"))
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


class LlmTests(unittest.TestCase):
    def test_json_object_mode_sends_schema_in_prompt_and_validates_response(self):
        with mock_api() as (url, requests):
            client = StructuredClient(ApiSettings(FAKE_KEY, "mock", url, response_mode="json_object"))
            self.assertEqual(client.complete("模拟通知抽取", {}), response())
            body = requests[0][2]
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertIn("JSON Schema", body["messages"][0]["content"])
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": '{"classification":"non_task","tasks":[],"warnings":[],"execute":"command"}'}}]}
        with mock_api(payload=envelope) as (url, _):
            with self.assertRaises(ModelError): StructuredClient(ApiSettings(FAKE_KEY, "mock", url, response_mode="json_object")).complete("模拟", {})

    def test_response_mode_is_explicit_and_invalid_mode_rejected(self):
        self.assertEqual(ApiSettings(FAKE_KEY, "mock").response_mode, "json_schema")
        with self.assertRaises(ModelError): ApiSettings(FAKE_KEY, "mock", response_mode="automatic")

    def test_relation_schema_is_sent_to_same_real_http_client(self):
        from course_assistant.matching import RELATION_SCHEMA
        result = {"kind": "new", "target_ids": [], "reason": "【模拟】独立任务", "evidence": "作业", "changes": []}
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
        with mock_api(payload=envelope) as (url, requests):
            client = StructuredClient(ApiSettings(FAKE_KEY, "mock-model", url), schema=RELATION_SCHEMA, schema_name="notice_relation")
            self.assertEqual(client.complete("【模拟】关系判断", {"untrusted_notice": "【模拟】作业"}), result)
            contract = requests[0][2]["response_format"]["json_schema"]
            self.assertEqual(contract["name"], "notice_relation")
            self.assertEqual(contract["schema"], RELATION_SCHEMA)

    def test_real_http_protocol_against_local_mock_service(self):
        with mock_api() as (url, requests):
            client = StructuredClient(ApiSettings(FAKE_KEY, "mock-model", url))
            report = AINotificationParser(client, settings()).analyze(Notice(RAW, SENT_AT), context())
            self.assertEqual(len(report.drafts), 1)
            path, auth, body = requests[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(auth, "Bearer " + FAKE_KEY)
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertNotIn("tools", body)
            self.assertNotIn(FAKE_KEY, json.dumps(body))
            self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])

    def test_api_settings_only_use_environment_and_hide_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY, "OPENAI_MODEL": "mock-model", "OPENAI_BASE_URL": "https://example.com/v1", "OPENAI_TIMEOUT_SECONDS": "12"}, clear=True):
            config = ApiSettings.from_env()
        self.assertEqual(config.timeout, 12)
        self.assertNotIn(FAKE_KEY, repr(config))
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ModelError, "OPENAI_API_KEY"):
            ApiSettings.from_env()

    def test_unsafe_url_and_invalid_timeouts_are_rejected(self):
        for url in ("http://example.com/v1", "https://user:secret@example.com", "https://example.com?key=secret", "https://[broken", "https://example.com:bad"):
            with self.subTest(url=url), self.assertRaises(ModelError): ApiSettings(FAKE_KEY, "mock", url)
        for timeout in (0, -1, float("nan"), float("inf"), 301):
            with self.subTest(timeout=timeout), self.assertRaises(ModelError): ApiSettings(FAKE_KEY, "mock", timeout=timeout)

    def test_http_errors_are_sanitized(self):
        for status in (400, 401, 403, 429, 500):
            error = urllib.error.HTTPError("https://example.com/" + FAKE_KEY, status, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode()))
            with patch("course_assistant.llm.urllib.request.build_opener") as builder:
                builder.return_value.open.side_effect = error
                with self.assertRaises(ModelError) as caught: StructuredClient(ApiSettings(FAKE_KEY, "mock")).complete("system", {})
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_timeout_and_connection_errors_are_sanitized(self):
        for error in (socket.timeout(FAKE_KEY), urllib.error.URLError(FAKE_KEY), OSError(FAKE_KEY)):
            with patch("course_assistant.llm.urllib.request.build_opener") as builder:
                builder.return_value.open.side_effect = error
                with self.assertRaises(ModelError) as caught: StructuredClient(ApiSettings(FAKE_KEY, "mock")).complete("system", {})
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_redirect_is_not_followed(self):
        with mock_api(status=302, location="https://example.com/do-not-follow") as (url, requests):
            with self.assertRaisesRegex(ModelError, "HTTP 302"): StructuredClient(ApiSettings(FAKE_KEY, "mock", url)).complete("system", {})
            self.assertEqual(len(requests), 1)

    def test_refusal_truncation_tools_and_bad_envelopes_are_rejected(self):
        values = [[], {}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": []}]},
                  {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
                  {"choices": [{"finish_reason": "stop", "message": {"refusal": "不解析", "content": None}}]},
                  {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "tool_calls": [{"name": "exec"}]}}]},
                  {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}]
        for payload in values:
            with self.subTest(payload=payload), patch("course_assistant.llm.urllib.request.build_opener") as builder:
                builder.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(ModelError): StructuredClient(ApiSettings(FAKE_KEY, "mock")).complete("system", {})

    def test_response_size_limit(self):
        with patch("course_assistant.llm.urllib.request.build_opener") as builder:
            builder.return_value.open.return_value.__enter__.return_value.read.return_value = b"a" * (2 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ModelError, "过大"): StructuredClient(ApiSettings(FAKE_KEY, "mock")).complete("system", {})
