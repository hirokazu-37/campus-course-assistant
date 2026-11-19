"""显式运行真实 API 验收；不写任务台账，不使用模拟客户端兜底。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from course_assistant.errors import ModelError, ValidationError
from course_assistant.interfaces import ParseContext
from course_assistant.llm import ApiSettings, StructuredClient
from course_assistant.models import Notice, aware_datetime
from course_assistant.parsing import AINotificationParser, ParserSettings


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")
    sample = json.loads((Path(__file__).resolve().parent.parent / "examples" / "api_cases.json").read_text(encoding="utf-8"))
    cases = sample["cases"]
    argument_parser = argparse.ArgumentParser(description="真实 API 验收：默认1次调用，--all 最多9次；全部输入均为模拟通知，不写台账")
    selection = argument_parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--case", choices=[case["id"] for case in cases], default="standard")
    args = argument_parser.parse_args()
    try:
        parser = AINotificationParser(StructuredClient(ApiSettings.from_env()), ParserSettings.load())
    except (ModelError, ValidationError, OSError) as exc:
        print(f"真实 API 验收未执行：{exc}", file=sys.stderr)
        return 2
    failures = 0
    for case in cases:
        if not args.all and case["id"] != args.case:
            continue
        try:
            notice = Notice(case["notice"], case["sent_at"])
            report = parser.analyze(notice, ParseContext(aware_datetime(notice.sent_at, "sent_at") if notice.sent_at else None,
                                                        "+08:00", tuple(sample["course_names"])))
            expected = case["expected"]
            passed = len(report.drafts) == expected["count"]
            if report.drafts:
                first = report.drafts[0]
                observed = {"course": first.course, "date": first.deadline.date, "time": first.deadline.time, "needs_confirmation": first.review.needs_confirmation}
                passed = passed and all(observed[key] == value for key, value in expected.items() if key != "count")
            failures += not passed
            print(json.dumps({"case": case["id"], "real_api_returned": True, "expectations_passed": passed,
                              "warnings": report.warnings, "drafts": [asdict(draft) for draft in report.drafts]}, ensure_ascii=False, indent=2))
        except (ModelError, ValidationError, OSError) as exc:
            failures += 1
            print(f"{case['id']}：验收未通过，{exc}", file=sys.stderr)
    print("以上仅为模拟通知的真实 API 验收，不是本人真实课程使用证据；没有写入台账。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
