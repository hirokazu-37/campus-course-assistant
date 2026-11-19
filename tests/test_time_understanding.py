from datetime import date, datetime
import unittest

from course_assistant.errors import ModelError
from course_assistant.time_understanding import explicit_clock, resolve_deadline
from tests.parser_fixtures import SENT_AT, deadline


class TimeUnderstandingTests(unittest.TestCase):
    def resolve(self, spec, sent_at=SENT_AT, monday=None):
        raw = "【模拟数据】" + (spec["expression"] or "") + (spec["time_expression"] or "")
        return resolve_deadline(spec, raw, datetime.fromisoformat(sent_at) if sent_at else None, monday)

    def test_next_wednesday_uses_message_week_instead_of_current_week(self):
        result, _ = self.resolve(deadline("week_day", "下周三", week_offset=1, weekday=3))
        self.assertEqual(result.date, "2026-11-25")

    def test_tomorrow_uses_shanghai_date_across_timezone_boundary(self):
        result, _ = self.resolve(deadline("relative_days", "明天", days=1), "2026-11-17T18:00:00+00:00")
        self.assertEqual(result.date, "2026-11-19")

    def test_relative_days_leap_year_and_year_boundary(self):
        for sent, word, days, expected in (("2024-02-28T10:00:00+08:00", "明天", 1, "2024-02-29"),
                                           ("2026-12-31T10:00:00+08:00", "后天", 2, "2027-01-02"),
                                           (SENT_AT, "三天后", 3, "2026-11-20")):
            result, _ = self.resolve(deadline("relative_days", word, days=days), sent)
            self.assertEqual(result.date, expected)

    def test_missing_sending_time_blocks_all_relative_kinds(self):
        specs = [deadline("relative_days", "明天", days=1), deadline("week_day", "下周三", week_offset=1, weekday=3),
                 deadline("semester_week", "第14周周三", semester_week=14, weekday=3)]
        for spec in specs:
            result, notes = self.resolve(spec, None, date(2026, 9, 7))
            self.assertIsNone(result.date)
            self.assertTrue(any("实际发送时间" in note for note in notes))

    def test_week14_needs_real_calendar_and_specific_day(self):
        spec = deadline("semester_week", "第14周", semester_week=14)
        result, notes = self.resolve(spec)
        self.assertIsNone(result.date)
        self.assertTrue(any("校历" in note for note in notes))
        result, notes = self.resolve(spec, monday=date(2026, 9, 7))
        self.assertIsNone(result.date)
        self.assertTrue(any("2026-12-07 至 2026-12-13" in note for note in notes))

    def test_week14_with_weekday_and_calendar_can_be_resolved(self):
        result, _ = self.resolve(deadline("semester_week", "第14周周三", semester_week=14, weekday=3), monday=date(2026, 9, 7))
        self.assertEqual(result.date, "2026-12-09")

    def test_bare_weekday_is_still_uncertain(self):
        result, _ = self.resolve(deadline("week_day", "周五", week_offset=0, weekday=5))
        self.assertIsNone(result.date)

    def test_missing_deadline_year_defaults_to_message_year(self):
        result, notes = self.resolve(deadline("absolute", "11月20日", date="2026-11-20"))
        self.assertEqual(result.date, "2026-11-20")
        self.assertTrue(any("消息发送年份2026" in note for note in notes))

    def test_ambiguous_clock_never_gets_invented(self):
        for text in ("晚上", "8点", "晚上12点", "八点左右", "20:00或21:00"):
            self.assertIsNone(explicit_clock(text))
        spec = deadline("week_day", "本周五晚上", week_offset=0, weekday=5, time="23:59", time_expression="晚上")
        result, _ = self.resolve(spec)
        self.assertEqual(result.precision, "date")
        self.assertIsNone(result.time)

    def test_exact_chinese_and_numeric_clocks(self):
        for text, expected in (("晚上八点半", "20:30"), ("下午三点十五分", "15:15"), ("二十三点", "23:00"), ("9:05", "09:05"), ("23:30:45", "23:30:45"), ("下午3:00", "15:00"), ("晚上8:30", "20:30")):
            self.assertEqual(explicit_clock(text), expected)

    def test_invalid_and_inconsistent_dates_are_rejected(self):
        specs = [deadline(date="2026-11-21"), deadline(expression="2026年2月30日", date="2026-02-30"),
                 deadline("relative_days", "明天", days=2), deadline("week_day", "下周三", week_offset=0, weekday=3),
                 deadline("semester_week", "第14周", semester_week=14, weekday=3),
                 deadline(date="2026-11-20", days=1), deadline(date="2026-11-20", time="25:00", time_expression="25:00")]
        for spec in specs:
            with self.subTest(spec=spec), self.assertRaises(ModelError): self.resolve(spec)

    def test_ambiguous_date_choices_are_not_reduced_to_one(self):
        result, notes = self.resolve(deadline("week_day", "本周五或本周六", week_offset=0, weekday=5))
        self.assertIsNone(result.date)
        self.assertTrue(notes)
