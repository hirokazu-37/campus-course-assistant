"""截止日期默认年：只用模拟响应/隔离目录，不是真实课程模型准确率证据。"""
from datetime import date, datetime
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from course_assistant.cache import CachedClient
from course_assistant.errors import ModelError
from course_assistant.models import Notice
from course_assistant.parsing import AINotificationParser
from course_assistant.schema import PARSE_SCHEMA
from course_assistant.time_understanding import deadline_default_year, resolve_deadline
from tests.parser_fixtures import FakeClient, RAW, context, deadline, response, settings


class DeadlineYearTests(unittest.TestCase):
    def resolve(self, expression="12月2日前", sent=date(2026, 11, 13), year=2026, **changes):
        spec = deadline("absolute", expression, **changes)
        return resolve_deadline(spec, "【模拟】" + expression, sent, default_year=year)

    def test_month_day_without_year_uses_message_year(self):
        due, notes = self.resolve()
        self.assertEqual(due.date, "2026-12-02")
        self.assertIsNone(due.time)
        self.assertEqual(due.precision, "date")
        self.assertTrue(any("消息发送年份2026" in note for note in notes))

    def test_old_notice_keeps_its_own_year(self):
        due, _ = self.resolve(sent=date(2024, 11, 13), year=2026)
        self.assertEqual(due.date, "2024-12-02")

    def test_missing_message_date_defaults_to_current_shanghai_year(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat("2026-11-18T10:00:00+08:00").astimezone(tz)
        with patch("course_assistant.time_understanding.datetime", FixedDatetime):
            year = deadline_default_year(None)
        due, notes = self.resolve(sent=None, year=year)
        self.assertEqual(due.date, "2026-12-02")
        self.assertTrue(any("当前上海年份2026" in note for note in notes))

    def test_message_year_obeys_shanghai_timezone(self):
        self.assertEqual(deadline_default_year(datetime.fromisoformat("2025-12-31T18:00:00Z")), 2026)

    def test_explicit_year_is_not_overridden(self):
        due, _ = self.resolve("2027年12月2日", date="2027-12-02")
        self.assertEqual(due.date, "2027-12-02")

    def test_complete_clock_not_flagged_just_for_omitted_year(self):
        due, notes = self.resolve("12月2日23:30", time="23:30", time_expression="23:30")
        self.assertEqual((due.date, due.time), ("2026-12-02", "23:30"))
        self.assertFalse(due.needs_confirmation)
        self.assertTrue(notes)  # 默认年依据是说明，不是错误。

    def test_next_year_qualifier_is_honored(self):
        due, _ = self.resolve("明年1月5日", sent=date(2026, 12, 20))
        self.assertEqual(due.date, "2027-01-05")

    def test_past_month_day_is_not_silently_rolled_to_next_year(self):
        due, notes = self.resolve("1月5日", sent=date(2026, 12, 20))
        self.assertEqual(due.date, "2026-01-05")
        self.assertTrue(due.needs_confirmation)
        self.assertTrue(any("跨年" in note for note in notes))

    def test_multiple_month_days_remain_uncertain(self):
        due, _ = self.resolve("12月2日、12月3日")
        self.assertIsNone(due.date)

    def test_invalid_month_day_not_moved_to_a_valid_year(self):
        for text in ("2月29日", "2月30日", "13月2日"):
            with self.subTest(text=text), self.assertRaises(ModelError):
                self.resolve(text)

    def test_leap_day_uses_actual_message_year(self):
        due, _ = self.resolve("2月29日", sent=date(2024, 2, 1))
        self.assertEqual(due.date, "2024-02-29")

    def test_model_cannot_choose_a_different_year(self):
        with self.assertRaises(ModelError):
            self.resolve(date="2027-12-02")

    def test_common_numeric_month_day_formats(self):
        for text in ("12/2", "12-02", "12.2"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text)[0].date, "2026-12-02")

    def test_parser_includes_default_year_in_model_context_and_keeps_explanation(self):
        raw = RAW + "12月2日前"
        client = FakeClient(response(deadline=deadline("absolute", "12月2日前", uncertain=True),
                                     notes=["截止日期“12月2日前”缺少年份，已标记为不确定。"], uncertain_fields=["deadline"]))
        parser = AINotificationParser(client, settings())
        report = parser.analyze(Notice(raw, "2026-11-13"), context("2026-11-13"))
        self.assertEqual(report.drafts[0].deadline.date, "2026-12-02")
        self.assertEqual(client.calls[0][1]["trusted_context"]["deadline_default_year"], 2026)
        self.assertFalse(any("缺少年份" in note for note in report.drafts[0].review.notes))
        self.assertTrue(any("消息发送年份2026" in note for note in report.drafts[0].review.notes))

    def test_current_year_in_cache_key_does_not_reuse_last_year_preview(self):
        with TemporaryDirectory() as folder:
            fake = FakeClient(response(deadline=deadline("absolute", "12月2日前", uncertain=True)))
            client = CachedClient(folder, lambda: fake, PARSE_SCHEMA, stage="parse")
            parser = AINotificationParser(client, settings())
            for year in (2026, 2027, 2027):
                with patch("course_assistant.parsing.deadline_default_year", return_value=year):
                    report = parser.analyze(Notice(RAW + "12月2日前"), context(None))
                self.assertEqual(report.drafts[0].deadline.date, f"{year}-12-02")
            self.assertEqual(len(fake.calls), 2)

    def test_default_year_does_not_supply_a_base_for_tomorrow(self):
        spec = deadline("relative_days", "明天", days=1)
        due, _ = resolve_deadline(spec, "【模拟】明天交", None, default_year=2026)
        self.assertIsNone(due.date)
