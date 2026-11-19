"""Streamlit AppTest：模拟通知、临时 JSON、回环模拟 API；不是本人真实使用证据。"""
from contextlib import redirect_stdout
from datetime import datetime
import importlib.util
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from course_assistant.llm import ApiSettings
from course_assistant.models import Deadline
from course_assistant.service import TaskService
from tests.parser_fixtures import RAW, SENT_AT
from tests.test_end_to_end import integration_api, FOLLOWUP, FOLLOWUP_SENT

HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None
if HAS_STREAMLIT:
    from streamlit.testing.v1 import AppTest

ENTRY = Path(__file__).resolve().parent.parent / "streamlit_app.py"
NOW = datetime.fromisoformat("2026-11-17T09:00:00+08:00")


@unittest.skipUnless(HAS_STREAMLIT, "可选界面依赖未安装；安装 requirements-ui.txt 后运行")
class WebUITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"COURSE_ASSISTANT_DATA_DIR": self.temp.name, "OPENAI_API_KEY": "", "OPENAI_MODEL": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.clock = patch("course_assistant.web_ui.now_shanghai", return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.service = TaskService(self.temp.name); self.service.initialize()
        self.app = AppTest.from_file(str(ENTRY), default_timeout=20)

    def run_app(self):
        self.app.run()
        self.assertFalse(self.app.exception, [item.message for item in self.app.exception])
        return self.app

    def nav(self, page):
        self.app.radio(key="page").set_value(page)
        self.run_app()

    def button(self, label):
        return next(item for item in self.app.button if item.label == label)

    def parse(self, text=RAW, sent=SENT_AT):
        self.app.text_area(key="notice_text").set_value(text)
        self.app.text_input(key="notice_sent_at").set_value(sent)
        self.button("AI 解析并预览").click(); self.run_app()

    def confirm(self):
        next(item for item in self.app.checkbox if item.label.startswith("我已核对以上任务")).check()
        self.run_app()
        self.button("确认并保存到任务台账").click(); self.run_app()

    def api(self, url):
        return patch("course_assistant.llm.ApiSettings.from_env", return_value=ApiSettings("not-a-real-key", "mock", url))

    def test_empty_pages_use_real_empty_files(self):
        before = (self.service.store.root / "deadlines.json").read_bytes()
        self.run_app()
        self.assertEqual([item.value for item in self.app.metric], ["0", "0", "0"])
        self.nav("通知导入"); self.nav("个人设置"); self.nav("任务总览")
        self.assertEqual((self.service.store.root / "deadlines.json").read_bytes(), before)

    def test_preview_confirmation_duplicate_and_completion_full_flow(self):
        with integration_api() as (url, requests), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse()
            self.assertEqual(self.service.list(), [])
            self.assertTrue(self.button("确认并保存到任务台账").disabled)
            self.run_app(); self.assertEqual(len(requests), 1)  # 刷新不付费重解析。
            self.confirm(); self.assertEqual(len(self.service.list()), 1)
            self.assertEqual(len(requests), 1)  # 提交不调用模型。
            self.parse(); self.assertEqual(len(requests), 1)
            self.assertTrue(any("已经处理" in item.value for item in self.app.info))
            self.nav("任务总览")
            self.app.toggle[0].set_value(True); self.run_app()
            self.assertEqual(self.service.list()[0].status, "completed")
            self.app.selectbox(key="task_filter").set_value("已完成"); self.run_app()
            self.app.toggle[0].set_value(False); self.run_app()
            self.assertEqual(self.service.list()[0].status, "pending")

    def test_chinese_date_only_preview_save_and_replay_without_another_api_call(self):
        with integration_api() as (url, requests), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse(sent="2026年11月13")
            self.assertFalse(self.app.error)
            self.assertTrue(any("2026-11-13" in item.value and "未补成零点" in item.value for item in self.app.text))
            self.assertEqual(self.service.list(), [])
            self.confirm()
            self.assertEqual(self.service.list()[0].source.sent_at, "2026-11-13")
            self.parse(sent="2026/11/13")
            self.assertEqual(len(requests), 1)
            self.assertTrue(any("已经处理" in item.value for item in self.app.info))

    def test_blank_input_uses_ai_header_time_and_displays_it_before_confirmation(self):
        raw = "发送日期：2026年11月13日\n" + RAW
        with integration_api() as (url, requests), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse(raw, "")
            self.assertFalse(self.app.error)
            self.assertTrue(any("AI从通知头部" in item.value for item in self.app.warning))
            self.assertEqual(len(requests), 2)  # 时间抽取+作业解析，均为本地模拟API。
            self.assertEqual(self.service.list(), [])
            self.confirm()
            saved = self.service.list()[0]
            self.assertEqual(saved.source.sent_at, "2026-11-13")
            self.assertTrue(any("AI从通知头部" in note for note in saved.review.notes))
            self.parse(raw, "")
            self.assertEqual(len(requests), 2)

    def test_omitted_deadline_year_uses_message_year_without_missing_year_warning(self):
        raw = "【模拟年份通知】中微：提交第3次作业，12月2日前，教学平台提交PDF。"
        with integration_api() as (url, requests), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse(raw, "2026年11月13")
            self.assertFalse(self.app.error)
            self.assertTrue(any("截止：2026-12-02" in item.value for item in self.app.text))
            self.assertFalse(any("缺少年份" in item.value for item in [*self.app.text, *self.app.warning]))
            self.assertEqual(self.service.list(), [])
            self.confirm()
            self.assertEqual(self.service.list()[0].deadline.date, "2026-12-02")
            self.assertEqual(self.service.list()[0].deadline.time, "23:59")
            self.parse(raw, "2026年11月13")
            self.assertEqual(len(requests), 1)

    def test_postponement_diff_rejection_then_confirmation_and_cached_retry(self):
        with integration_api() as (url, requests), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse(); self.confirm()
            old = self.service.list()[0]
            self.parse(FOLLOWUP, FOLLOWUP_SENT)
            self.assertTrue(any("延期" in item.value for item in self.app.info))
            self.assertGreater(len(self.app.dataframe), 0)
            self.assertEqual(self.service.get(old.id).deadline.date, "2026-11-20")
            self.button("取消本次预览").click(); self.run_app()
            self.assertEqual(self.service.get(old.id).deadline.date, "2026-11-20")
            self.parse(FOLLOWUP, FOLLOWUP_SENT); self.assertEqual(len(requests), 3)
            self.confirm()
            self.assertEqual(self.service.get(old.id).deadline.date, "2026-11-22")
            self.assertEqual(len(self.service.list()), 1)
            self.assertEqual(len(requests), 3)

    def test_stale_preview_not_saved(self):
        with integration_api() as (url, _), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse()
            self.service.add(course="【模拟】其他课程", title="【模拟】并发新增")
            self.confirm()
            self.assertEqual(len(self.service.list()), 1)
            self.assertTrue(any("台账已变化" in item.value for item in self.app.error))

    def test_missing_api_key_and_empty_input_show_errors_without_writing(self):
        self.run_app(); self.nav("通知导入"); self.parse("", "")
        self.assertTrue(self.app.error)
        self.parse()
        self.assertTrue(any("OPENAI_API_KEY" in item.value for item in self.app.error))
        self.assertEqual(self.service.list(), [])

    def test_failed_new_parse_clears_previous_preview(self):
        with integration_api() as (url, _), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse()
            self.assertIsNotNone(self.app.session_state["notice_preview"])
            self.parse("", "")
            self.assertNotIn("notice_preview", self.app.session_state)
            self.assertFalse(any(item.label == "确认并保存到任务台账" for item in self.app.button))

    def test_uncertain_relation_disables_save(self):
        from dataclasses import replace
        from course_assistant.interfaces import TaskDraft
        from course_assistant.models import Notice, Submission
        from course_assistant.workflow import NoticePlan, PlannedAction
        draft = TaskDraft("【模拟】微观", "【模拟】作业", "", Deadline(), Submission(), Notice("【模拟】上次作业延期"))
        plan = NoticePlan("a" * 64, "revision", (PlannedAction("uncertain", draft, (), "多个候选，需核实"),))
        with patch("course_assistant.dashboard.preview_notice", return_value=plan):
            self.run_app(); self.nav("通知导入"); self.parse("【模拟】上次作业延期", "")
            next(item for item in self.app.checkbox if item.label.startswith("我已核对以上任务")).check(); self.run_app()
            self.assertTrue(self.button("确认并保存到任务台账").disabled)
            self.assertEqual(self.service.list(), [])

    def test_preferences_and_course_persist_in_existing_files(self):
        self.run_app(); self.nav("个人设置")
        self.app.toggle[0].set_value(True)
        next(item for item in self.app.text_input if item.label == "提前几天提醒").set_value("5, 2")
        self.button("保存个人设置").click(); self.run_app()
        self.assertEqual(self.service.config()["reminder_preferences"]["lead_days"], [5, 2])
        self.assertTrue(self.service.config()["reminders_enabled"])
        next(item for item in self.app.text_input if item.label == "添加标准课程名称").set_value("【模拟】计量经济学")
        self.button("保存课程").click(); self.run_app()
        self.assertEqual(self.service.courses()[0]["name"], "【模拟】计量经济学")

    def test_invalid_preferences_do_not_change_file(self):
        self.run_app(); self.nav("个人设置")
        before = (self.service.store.root / "config.json").read_bytes()
        next(item for item in self.app.text_input if item.label == "提前几天提醒").set_value("3, 3")
        self.button("保存个人设置").click(); self.run_app()
        self.assertTrue(self.app.error)
        self.assertEqual((self.service.store.root / "config.json").read_bytes(), before)

    def test_manual_reminder_button_uses_runner_but_rendering_does_not(self):
        with patch("course_assistant.web_ui.ReminderRunner") as runner:
            runner.return_value.check.return_value = {"count": 0, "delivery": None, "events": [], "log": "【模拟】日志"}
            self.run_app(); self.run_app(); runner.assert_not_called()
            self.button("执行一次提醒检查").click(); self.run_app()
            runner.return_value.check.assert_called_once_with(trigger="streamlit")

    def test_manual_deadline_change_requires_diff_and_consent(self):
        task = self.service.add(course="【模拟】课程", title="【模拟】作业")
        self.run_app()
        next(item for item in self.app.text_input if item.label == "核实后的截止时间").set_value("2026-11-23T12:00")
        self.button("预览截止时间修改").click(); self.run_app()
        self.assertIsNone(self.service.get(task.id).deadline.date)
        self.assertTrue(self.button("确认保存截止时间").disabled)
        next(item for item in self.app.checkbox if item.label == "我已核实以上截止时间修改").check(); self.run_app()
        self.button("确认保存截止时间").click(); self.run_app()
        self.assertEqual(self.service.get(task.id).deadline.date, "2026-11-23")

    def test_corrupt_ledger_shows_error_and_preserves_bytes(self):
        path = self.service.store.root / "deadlines.json"; path.write_bytes(b'{"broken":')
        self.run_app()
        self.assertTrue(self.app.error)
        self.assertEqual(path.read_bytes(), b'{"broken":')

    def test_notice_is_displayed_as_plain_text(self):
        raw = '<script>alert("模拟注入")</script> [模拟链接](https://example.invalid)'
        self.service.add(course="【模拟】课程", title=raw)
        self.run_app()
        self.assertTrue(any(item.value == raw for item in self.app.text))
        self.assertFalse(any(raw in item.value for item in self.app.markdown))

    def configure_session_api(self, url, key="mock-session-key-never-real"):
        self.app.text_input(key="api_key_input").set_value(key)
        self.app.text_input(key="api_model_input").set_value("mock-session-model")
        self.app.text_input(key="api_base_input").set_value(url)
        self.button("保存本次会话连接").click(); self.run_app()

    def test_session_api_is_used_and_secret_never_saved_or_displayed(self):
        from tests.test_llm import mock_api
        secret = "mock-session-key-never-real"
        with mock_api() as (url, requests):
            self.run_app(); self.nav("个人设置")
            before = (self.service.store.root / "config.json").read_bytes()
            self.configure_session_api(url, secret)
            self.assertFalse(requests)  # 保存连接不请求付费 API。
            self.assertEqual(self.app.text_input(key="api_key_input").value, "")
            self.assertNotIn(secret, repr(self.app.session_state["api_settings"]))
            self.assertEqual((self.service.store.root / "config.json").read_bytes(), before)
            self.nav("通知导入"); self.parse()
            self.assertEqual(requests[0][1], "Bearer " + secret)
            self.assertEqual(requests[0][2]["model"], "mock-session-model")
            for path in self.service.store.root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes(), str(path))
            for kind in (self.app.text, self.app.markdown, self.app.caption, self.app.error):
                self.assertFalse(any(secret in str(item.value) for item in kind))
            self.assertNotIn(secret, os.environ.values())

    def test_session_api_rejects_unsafe_address_and_does_not_echo_key(self):
        self.run_app(); self.nav("个人设置")
        self.configure_session_api("http://remote.invalid", "mock-secret-invalid-url")
        self.assertTrue(self.app.error)
        self.assertNotIn("api_settings", self.app.session_state)
        self.assertFalse(any("mock-secret-invalid-url" in item.value for item in self.app.error))

    def test_session_api_can_be_cleared_and_is_not_shared_between_sessions(self):
        self.run_app(); self.nav("个人设置")
        self.configure_session_api("https://example.invalid/v1")
        other = AppTest.from_file(str(ENTRY), default_timeout=20).run()
        self.assertNotIn("api_settings", other.session_state)
        self.button("清除本次会话连接").click(); self.run_app()
        self.assertNotIn("api_settings", self.app.session_state)
        self.assertEqual(self.app.text_input(key="api_key_input").value, "")

    def test_repreview_resets_confirmation(self):
        with integration_api() as (url, _), self.api(url):
            self.run_app(); self.nav("通知导入"); self.parse()
            next(item for item in self.app.checkbox if item.label.startswith("我已核对以上任务")).check(); self.run_app()
            self.assertFalse(self.button("确认并保存到任务台账").disabled)
            self.parse()
            self.assertTrue(self.button("确认并保存到任务台账").disabled)

    def test_manual_resolution_repreview_then_cancel_has_no_stale_form(self):
        from course_assistant.interfaces import TaskDraft
        from course_assistant.models import Notice, Submission
        from course_assistant.workflow import NoticePlan, PlannedAction
        draft = TaskDraft("【模拟】微观", "【模拟】作业", "", Deadline(), Submission(), Notice("【模拟】上次作业延期"))
        uncertain = NoticePlan("a" * 64, "revision", (PlannedAction("uncertain", draft, (), "需核实"),))
        new = NoticePlan("a" * 64, "revision", (PlannedAction("new", draft, (), "人工选择新建"),))
        with patch("course_assistant.dashboard.preview_notice", side_effect=[uncertain, new]):
            self.run_app(); self.nav("通知导入"); self.parse("【模拟】上次作业延期", "")
            next(item for item in self.app.selectbox if item.label == "这条通知应如何处理").set_value("明确作为新任务")
            self.button("按人工选择重新预览").click(); self.run_app()
            self.assertTrue(self.button("确认并保存到任务台账").disabled)
            self.button("取消本次预览").click(); self.run_app()
            self.assertEqual(self.service.list(), [])

    def test_failed_status_write_restores_displayed_toggle(self):
        from course_assistant.errors import StorageError
        task = self.service.add(course="【模拟】课程", title="【模拟】作业")
        self.run_app()
        with patch("course_assistant.service.TaskService.set_completed", side_effect=StorageError("模拟保存失败")):
            self.app.toggle[0].set_value(True); self.run_app()
        self.assertTrue(self.app.error)
        self.assertFalse(self.app.toggle[0].value)
        self.assertEqual(self.service.get(task.id).status, "pending")

    def test_course_filter_sort_and_future_seven_days(self):
        self.service.add(course="【模拟】A", title="【模拟】较晚作业", deadline=Deadline.from_input("2026-11-22T12:00"))
        self.service.add(course="【模拟】B", title="【模拟】较早作业", deadline=Deadline.from_input("2026-11-18T12:00"))
        self.service.add(course="【模拟】B", title="【模拟】远期作业", deadline=Deadline.from_input("2026-12-01T12:00"))
        self.run_app()
        titles = [item.value for item in self.app.text if item.value.endswith("作业")]
        self.assertEqual(titles, ["【模拟】较早作业", "【模拟】较晚作业", "【模拟】远期作业"])
        next(item for item in self.app.selectbox if item.label == "排序与范围").set_value("未来7天"); self.run_app()
        self.assertFalse(any(item.value == "【模拟】远期作业" for item in self.app.text))
        next(item for item in self.app.selectbox if item.label == "按课程筛选").set_value("【模拟】A"); self.run_app()
        self.assertTrue(any(item.value == "【模拟】较晚作业" for item in self.app.text))
        self.assertFalse(any(item.value == "【模拟】较早作业" for item in self.app.text))

    def test_settings_have_course_entry_without_alias_table_or_sidebar_navigation(self):
        self.run_app(); self.nav("个人设置")
        self.assertTrue(any(item.label == "添加标准课程名称" for item in self.app.text_input))
        self.assertFalse(any("课程别名" in item.label for item in self.app.expander))
        self.assertEqual(len(self.app.sidebar.radio), 0)
        self.assertEqual(self.app.radio(key="page").options, ["任务总览", "通知导入", "个人设置"])
