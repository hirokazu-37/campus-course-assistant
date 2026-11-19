"""用户课程名单 + 模拟AI简称推断；不维护规则，不代表真实模型准确率。"""
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from course_assistant.errors import ModelError
from course_assistant.interfaces import ParseContext
from course_assistant.models import Notice
from course_assistant.parsing import AINotificationParser, ParserSettings
from tests.parser_fixtures import FakeClient, SENT_AT, response

RAW = "【模拟通知】概统：提交第3次作业，2026年11月20日23:30截止，教学平台提交PDF。"


class CourseInferenceTests(unittest.TestCase):
    def analyze(self, course="概率统计", quote="概统", names=("概率统计", "财务会计"), uncertain=(), raw=RAW, config=None):
        client = FakeClient(response(course=course, course_evidence=quote, uncertain_fields=list(uncertain)))
        parser = AINotificationParser(client, config or ParserSettings())
        draft = parser.analyze(Notice(raw, SENT_AT), ParseContext(datetime.fromisoformat(SENT_AT), "+08:00", names)).drafts[0]
        return draft, client

    def test_model_maps_abbreviation_without_any_alias_dictionary(self):
        draft, client = self.analyze()
        self.assertEqual(draft.course, "概率统计")
        self.assertNotIn("course", draft.review.uncertain_fields)
        self.assertTrue(any("概统" in note and "概率统计" in note for note in draft.review.notes))
        self.assertEqual(client.calls[0][1]["trusted_context"]["course_names"], ["概率统计", "财务会计"])
        self.assertEqual(client.calls[0][1]["trusted_context"]["course_aliases"], {})

    def test_other_course_abbreviation_is_inferred_by_same_interface(self):
        draft, _ = self.analyze(course="财务会计", quote="财会", raw=RAW.replace("概统", "财会"))
        self.assertEqual(draft.course, "财务会计")
        self.assertNotIn("course", draft.review.uncertain_fields)

    def test_ambiguous_abbreviation_stays_pending_even_if_model_suggests_name(self):
        for selected in (None, "财务会计"):
            with self.subTest(selected=selected):
                draft, _ = self.analyze(course=selected, quote="会计", names=("财务会计", "成本会计"),
                                        uncertain=("course",), raw=RAW.replace("概统", "会计"))
                self.assertEqual(draft.course, "会计")
                self.assertIn("course", draft.review.uncertain_fields)

    def test_model_cannot_invent_unregistered_canonical_course(self):
        draft, _ = self.analyze(course="模型编造的课程")
        self.assertEqual(draft.course, "概统")
        self.assertIn("course", draft.review.uncertain_fields)

    def test_single_registered_course_does_not_fill_missing_course(self):
        draft, _ = self.analyze(course=None, quote="", names=("概率统计",), raw=RAW.replace("概统：", ""))
        self.assertEqual(draft.course, "待确认课程")
        self.assertIn("course", draft.review.uncertain_fields)

    def test_missing_roster_keeps_original_name_and_requests_registration(self):
        draft, _ = self.analyze(names=())
        self.assertEqual(draft.course, "概统")
        self.assertIn("course", draft.review.uncertain_fields)
        self.assertTrue(any("填写课程全名" in note for note in draft.review.notes))

    def test_full_course_name_is_grounded_despite_wrong_model_canonical_name(self):
        draft, _ = self.analyze(course="财务会计", quote="概率统计", raw=RAW.replace("概统", "概率统计"))
        self.assertEqual(draft.course, "概率统计")

    def test_course_evidence_must_exist_in_raw_notice(self):
        with self.assertRaises(ModelError):
            self.analyze(quote="原文没有的简称")

    def test_legacy_mapping_cannot_override_registered_course_list(self):
        draft, _ = self.analyze(config=ParserSettings({"概统": "旧规则里的错误课程"}))
        self.assertEqual(draft.course, "概率统计")

    def test_new_settings_load_without_aliases_and_legacy_settings_still_load(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "parsing.json"
            data = {"schema_version": 1, "timezone": "Asia/Shanghai", "semester_week1_monday": None}
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(ParserSettings.load(path).aliases, {})
            data["course_aliases"] = {"概统": "概率统计"}
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(ParserSettings.load(path).aliases["概统"], "概率统计")

    def test_project_default_settings_have_no_preset_courses(self):
        self.assertEqual(ParserSettings.load().aliases, {})
