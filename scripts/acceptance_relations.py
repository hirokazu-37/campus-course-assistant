"""模拟通知的真实 API 关系验收，最多两次调用，仅在临时目录预览。"""
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys

from course_assistant.cache import CachedClient
from course_assistant.errors import ModelError, ValidationError, StorageError
from course_assistant.interfaces import ParseContext
from course_assistant.llm import ApiSettings, StructuredClient
from course_assistant.matching import RELATION_SCHEMA, SemanticMatcher
from course_assistant.models import Deadline, Notice, Submission, aware_datetime
from course_assistant.parsing import AINotificationParser, ParserSettings
from course_assistant.schema import PARSE_SCHEMA
from course_assistant.service import TaskService
from course_assistant.workflow import NoticeWorkflow


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")
    try:
        api = ApiSettings.from_env()
    except ModelError as exc:
        print(f"真实 API 关系验收未执行：{exc}", file=sys.stderr)
        return 2
    try:
        with TemporaryDirectory(prefix="course-relations-acceptance-") as folder:
            root = Path(folder)
            service = TaskService(root); service.initialize()
            old = service.add(course="微观经济学", title="微观经济学作业", description="完成课后作业",
                              deadline=Deadline.from_input("2026-11-25"), submission=Submission("教学平台", "PDF"),
                              source=Notice("【模拟验收数据】微观经济学作业11月25日截止。", "2026-11-17T18:00:00+08:00"))
            settings = ParserSettings.load()
            parser = AINotificationParser(CachedClient(root, lambda: StructuredClient(api), PARSE_SCHEMA, stage="parse"), settings)
            matcher = SemanticMatcher(CachedClient(root, lambda: StructuredClient(api, schema=RELATION_SCHEMA, schema_name="notice_relation"),
                                                   RELATION_SCHEMA, stage="relations"), settings.aliases)
            workflow = NoticeWorkflow(service, parser, matcher)
            notice = Notice("【模拟验收数据】上次的微观作业延期到11月27日，其他要求不变。", "2026-11-18T18:00:00+08:00")
            plan = workflow.prepare(notice, ParseContext(aware_datetime(notice.sent_at, "sent_at"), "+08:00", (old.course,)))
            passed = (len(plan.actions) == 1 and plan.actions[0].kind == "update" and plan.actions[0].target_ids == (old.id,) and
                      plan.actions[0].patch.get("deadline") is not None and plan.actions[0].patch["deadline"].date == "2026-11-27")
            print(json.dumps({"expectations_passed": passed, "actions": [action.preview() for action in plan.actions]}, ensure_ascii=False, indent=2))
            print("模拟通知的真实 API 预览已返回；没有执行更新，没有写入正式台账，不是本人真实用例证据。")
            return 0 if passed else 1
    except (ModelError, ValidationError, StorageError, OSError) as exc:
        print(f"真实 API 关系验收未通过：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
