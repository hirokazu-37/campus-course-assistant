"""全部为模拟模型输出，不能作为真实 API 调用或本人作业用例证据。"""
from copy import deepcopy
from datetime import datetime

from course_assistant.interfaces import ParseContext
from course_assistant.models import notice_time
from course_assistant.parsing import ParserSettings

RAW = "【模拟数据】中微课程：提交第3次作业，2026年11月20日23:30截止，教学平台提交PDF。"
SENT_AT = "2026-11-17T18:00:00+08:00"


def context(sent_at=SENT_AT):
    return ParseContext(notice_time(sent_at), "+08:00", ())


def settings():
    return ParserSettings({"中微": "中级微观经济学", "微观": "中级微观经济学", "中级微观经济学": "中级微观经济学"}, date_only_deadline_time=None)


def deadline(kind="absolute", expression="2026年11月20日23:30", **changes):
    value = {"kind": kind, "expression": expression, "date": None, "days": None, "week_offset": None,
             "weekday": None, "semester_week": None, "time": None, "time_expression": None, "uncertain": False}
    value.update(changes)
    return value


def response(**changes):
    item = {"course": "中级微观经济学", "course_evidence": "中微", "title": "【模拟】第3次作业", "description": "完成第3次作业",
            "task_evidence": "提交第3次作业", "deadline": deadline(date="2026-11-20", time="23:30", time_expression="23:30"),
            "submission_method": "教学平台", "submission_evidence": "教学平台", "format_requirements": "PDF", "format_evidence": "PDF",
            "uncertain_fields": [], "notes": []}
    item.update(changes)
    return {"classification": "tasks", "tasks": [item], "warnings": []}


class FakeClient:
    def __init__(self, output):
        self.output, self.calls = output, []

    def complete(self, system_prompt, payload):
        self.calls.append((system_prompt, payload))
        return deepcopy(self.output)
