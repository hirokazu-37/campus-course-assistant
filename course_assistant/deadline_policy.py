"""用户明确选择的日期末默认时刻；约定不伪装为老师原文。"""
from __future__ import annotations

from datetime import time
import re

from .errors import ValidationError
from .models import Deadline, TaskReview

DEFAULT_DATE_ONLY_TIME = "23:59"


def validate_default_time(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        point = time.fromisoformat(value)
        if point.tzinfo or point.isoformat(timespec="minutes") != value:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError("date_only_deadline_time 必须为HH:MM或null") from None
    return value


def missing_clock_only(note: str) -> bool:
    if re.search(r"发送|跨年|冲突|歧义|可能|左右|待定|多个|多种|另行通知", note):
        return False
    return bool(re.search(r"(?:无|未写|未注明|未提供|未明确|没有|缺少|尚无).{0,8}(?:钟点|时刻|时间)|(?:钟点|时刻).{0,3}(?:未知|未提供)|只有截止日期", note))


def date_has_uncertainty(notes) -> bool:
    for note in notes:
        if missing_clock_only(note):
            continue
        if re.search(r"日期|截止|年份|跨年", note) and re.search(r"跨年|冲突|歧义|可能|左右|待定|多个|多种|另行通知|无法确定", note):
            return True
    return False


def default_clock_note(clock: str) -> str:
    return f"原通知未提供具体截止钟点；按用户默认规则采用当天 {clock}（个人约定，并非老师明示）"


def review_with_default_clock(review: TaskReview, clock: str) -> TaskReview:
    fields = tuple(field for field in review.uncertain_fields if field != "deadline")
    notes = tuple(dict.fromkeys([*(note for note in review.notes if not missing_clock_only(note)), default_clock_note(clock)]))
    return TaskReview(bool(fields), fields, notes)


def with_default_clock(deadline: Deadline, clock: str) -> Deadline:
    validate_default_time(clock)
    if deadline.precision != "date" or deadline.date is None:
        raise ValidationError("只有已知日期、未填钟点的任务可以采用默认截止时刻")
    return Deadline.from_input(deadline.date + "T" + clock, deadline.utc_offset)
