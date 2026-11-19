"""统一任务模型；所有日期都显式保留精度，不推测通知含义。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Any
from uuid import UUID, uuid4

from .errors import ValidationError


def nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 必须是非空字符串")
    return value.strip()


def text_value(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    return value


def exact_keys(value: Any, keys: set[str], field: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError(f"{field} 字段不完整或包含未知字段")
    return value


def parse_offset(value: str) -> timezone:
    try:
        if not isinstance(value, str) or re.fullmatch(r"[+-][0-9]{2}:[0-9]{2}", value) is None:
            raise ValueError
        hours, minutes = int(value[1:3]), int(value[4:6])
        if not (0 <= hours <= 14 and 0 <= minutes <= 59) or (hours == 14 and minutes):
            raise ValueError
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if value[0] == "+" else -delta)
    except (ValueError, TypeError) as exc:
        raise ValidationError("utc_offset 必须为 ±HH:MM，例如 +08:00（最大 ±14:00）") from exc


def aware_datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(nonempty(value, field))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"{field} 必须是包含时区的 ISO 日期时间") from exc


def notice_time(value: Any) -> date | datetime | None:
    """通知允许仅知日期；不把日期补成实际发送的午夜。旧时间戳保持兼容。"""
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValidationError("消息发送日期不是有效日期") from None
    return aware_datetime(value, "source.sent_at")


def notice_time_bounds(value: str) -> tuple[datetime, datetime]:
    """仅用于比较先后顺序的区间，区间端点不写回通知。"""
    point = notice_time(value)
    if isinstance(point, datetime):
        return point, point
    if isinstance(point, date):
        zone = parse_offset("+08:00")
        return datetime.combine(point, time.min, zone), datetime.combine(point, time.max, zone)
    raise ValidationError("通知发送日期未知，不能比较先后")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Deadline:
    date: str | None = None
    time: str | None = None
    utc_offset: str = "+08:00"
    precision: str = "unknown"
    needs_confirmation: bool = True

    def __post_init__(self) -> None:
        parse_offset(self.utc_offset)
        if type(self.needs_confirmation) is not bool:
            raise ValidationError("needs_confirmation 必须为布尔值")
        if self.precision not in ("unknown", "date", "minute", "second"):
            raise ValidationError("时间精度必须为 unknown/date/minute/second")
        try:
            if self.precision == "unknown":
                if self.date is not None or self.time is not None or not self.needs_confirmation:
                    raise ValueError
                return
            if not isinstance(self.date, str) or date.fromisoformat(self.date).isoformat() != self.date:
                raise ValueError
            if self.precision == "date":
                if self.time is not None or not self.needs_confirmation:
                    raise ValueError
            else:
                expected_length = 5 if self.precision == "minute" else 8
                if not isinstance(self.time, str) or len(self.time) != expected_length:
                    raise ValueError
                parsed = time.fromisoformat(self.time)
                if parsed.tzinfo is not None or parsed.isoformat(timespec="minutes" if expected_length == 5 else "seconds") != self.time:
                    raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValidationError("截止日期/时间与精度不匹配；仅日期或未知时间必须待确认") from exc

    @classmethod
    def from_input(cls, value: str | None, offset: str = "+08:00", *, needs_confirmation: bool = False) -> Deadline:
        if value is None:
            return cls(utc_offset=offset)
        if not isinstance(value, str):
            raise ValidationError("截止时间必须是 ISO 字符串")
        if "T" not in value:
            return cls(value, None, offset, "date", True)
        day, clock = value.split("T", 1)
        precision = "minute" if len(clock) == 5 else "second"
        return cls(day, clock, offset, precision, needs_confirmation)

    @classmethod
    def from_dict(cls, value: Any) -> Deadline:
        exact_keys(value, {"date", "time", "utc_offset", "precision", "needs_confirmation"}, "deadline")
        return cls(**value)

    def sort_key(self) -> tuple:
        # 仅日期按当日 00:00 排列，但绝不把该排序键写回为真实截止时间。
        if self.date is None:
            return (1, datetime.max.replace(tzinfo=timezone.utc))
        point = datetime.combine(date.fromisoformat(self.date), time.fromisoformat(self.time) if self.time else time.min, parse_offset(self.utc_offset))
        return (0, point.astimezone(timezone.utc))


@dataclass(frozen=True)
class Submission:
    method: str = ""
    format_requirements: str = ""

    def __post_init__(self) -> None:
        text_value(self.method, "submission.method")
        text_value(self.format_requirements, "submission.format_requirements")

    @classmethod
    def from_dict(cls, value: Any) -> Submission:
        exact_keys(value, {"method", "format_requirements"}, "submission")
        return cls(**value)


@dataclass(frozen=True)
class Notice:
    raw_notice: str = ""
    sent_at: str | None = None

    def __post_init__(self) -> None:
        text_value(self.raw_notice, "source.raw_notice")
        if self.sent_at is not None:
            notice_time(self.sent_at)

    @classmethod
    def from_dict(cls, value: Any) -> Notice:
        exact_keys(value, {"raw_notice", "sent_at"}, "source")
        return cls(**value)


@dataclass(frozen=True)
class Change:
    at: str
    action: str
    changes: dict[str, Any]

    def __post_init__(self) -> None:
        aware_datetime(self.at, "history.at")
        if self.action not in ("created", "completed", "deadline_changed", "task_updated", "task_deleted", "undone"):
            raise ValidationError("未知的任务历史动作")
        if not isinstance(self.changes, dict):
            raise ValidationError("history.changes 必须是对象")

    @classmethod
    def from_dict(cls, value: Any) -> Change:
        exact_keys(value, {"at", "action", "changes"}, "history entry")
        return cls(**deepcopy(value))


@dataclass(frozen=True)
class TaskReview:
    needs_confirmation: bool = False
    uncertain_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        allowed = {"course", "title", "description", "deadline", "submission.method", "submission.format_requirements"}
        if type(self.needs_confirmation) is not bool or self.needs_confirmation != bool(self.uncertain_fields):
            raise ValidationError("review 待确认标记与字段列表不一致")
        if not isinstance(self.uncertain_fields, tuple) or any(not isinstance(v, str) or v not in allowed for v in self.uncertain_fields):
            raise ValidationError("review 包含未知的待确认字段")
        if not isinstance(self.notes, tuple) or any(not isinstance(v, str) or not v.strip() for v in self.notes):
            raise ValidationError("review.notes 必须是非空字符串列表")

    @classmethod
    def from_dict(cls, value: Any) -> TaskReview:
        exact_keys(value, {"needs_confirmation", "uncertain_fields", "notes"}, "review")
        if not isinstance(value["uncertain_fields"], list) or not isinstance(value["notes"], list):
            raise ValidationError("review 字段列表必须为数组")
        return cls(value["needs_confirmation"], tuple(value["uncertain_fields"]), tuple(value["notes"]))


@dataclass(frozen=True)
class Task:
    id: str
    course: str
    title: str
    description: str
    deadline: Deadline
    submission: Submission
    source: Notice
    status: str
    created_at: str
    updated_at: str
    completed_at: str | None
    history: tuple[Change, ...]
    review: TaskReview = TaskReview()
    deleted_at: str | None = None

    def __post_init__(self) -> None:
        try:
            if str(UUID(self.id)) != self.id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("任务 ID 必须是标准 UUID") from exc
        if nonempty(self.course, "course") != self.course or nonempty(self.title, "title") != self.title:
            raise ValidationError("课程和作业名称不能含首尾空白")
        text_value(self.description, "description")
        if not isinstance(self.review, TaskReview):
            raise ValidationError("review 类型不正确")
        if not isinstance(self.deadline, Deadline) or not isinstance(self.submission, Submission) or not isinstance(self.source, Notice):
            raise ValidationError("任务的 deadline/submission/source 类型不正确")
        created = aware_datetime(self.created_at, "created_at")
        updated = aware_datetime(self.updated_at, "updated_at")
        if updated < created:
            raise ValidationError("updated_at 不能早于 created_at")
        if self.deleted_at is not None and not created <= aware_datetime(self.deleted_at, "deleted_at") <= updated:
            raise ValidationError("deleted_at 必须在创建和最近修改时间之间")
        if self.status not in ("pending", "completed"):
            raise ValidationError("status 必须为 pending 或 completed")
        if self.status == "completed":
            if self.completed_at is None or not created <= aware_datetime(self.completed_at, "completed_at") <= updated:
                raise ValidationError("已完成任务需要有效的 completed_at")
        elif self.completed_at is not None:
            raise ValidationError("待办任务不能有 completed_at")
        if not isinstance(self.history, tuple) or not self.history or not all(isinstance(entry, Change) for entry in self.history):
            raise ValidationError("任务必须包含变更历史")
        if self.history[0].action != "created" or self.history[0].at != self.created_at or self.history[-1].at != self.updated_at:
            raise ValidationError("任务历史与创建/修改时间不一致")
        previous = created
        for entry in self.history:
            current = aware_datetime(entry.at, "history.at")
            if not previous <= current <= updated:
                raise ValidationError("任务历史必须按时间顺序排列")
            previous = current

    @classmethod
    def create(cls, *, course: str, title: str, description: str = "", deadline: Deadline | None = None,
               submission: Submission | None = None, source: Notice | None = None, review: TaskReview | None = None) -> Task:
        now = timestamp()
        return cls(str(uuid4()), nonempty(course, "course"), nonempty(title, "title"), description,
                   deadline if deadline is not None else Deadline(),
                   submission if submission is not None else Submission(), source if source is not None else Notice(),
                   "pending", now, now, None, (Change(now, "created", {"status": {"before": None, "after": "pending"}}),),
                   review if review is not None else TaskReview())

    def complete(self) -> Task:
        if self.deleted_at is not None:
            raise ValidationError("已删除任务不能标记完成，请先撤销删除")
        if self.status == "completed":
            return self
        return self.mutate({"status": "completed", "completed_at": timestamp()}, "completed")

    def set_deadline(self, deadline: Deadline) -> Task:
        review = self.review
        if not deadline.needs_confirmation and "deadline" in review.uncertain_fields:
            fields = tuple(field for field in review.uncertain_fields if field != "deadline")
            review = TaskReview(bool(fields), fields, review.notes)
        if deadline == self.deadline and review == self.review: return self
        if self.deleted_at is not None:
            raise ValidationError("已删除任务不能修改，请先撤销删除")
        return self.mutate({"deadline": deadline, "review": review}, "deadline_changed")

    def state(self) -> dict:
        return {key: value for key, value in self.to_dict().items() if key not in ("id", "created_at", "updated_at", "history")}

    def mutate(self, values: dict, action: str, *, notice: Notice | None = None, reason: str = "") -> Task:
        allowed = {"course", "title", "description", "deadline", "submission", "review", "status", "completed_at", "deleted_at"}
        if set(values) - allowed:
            raise ValidationError("修改包含不允许的字段")
        if all(getattr(self, key) == value for key, value in values.items()):
            return self
        now = timestamp()
        if action == "completed":
            values = {**values, "completed_at": now}
        provisional = replace(self, **values, updated_at=now,
                              history=(*self.history, Change(now, action, {})))
        before, after = self.state(), provisional.state()
        changes = {key: {"before": before[key], "after": after[key]} for key in before if before[key] != after[key]}
        changes.update({"_before": before, "_after": after, "reason": reason})
        if notice is not None:
            changes["notice"] = asdict(notice)
        return replace(provisional, history=(*self.history, Change(now, action, changes)))

    def undo(self) -> Task:
        """只撤销最近一项实际修改；追加记录，不删除历史，不提供隐式 redo。"""
        change = self.history[-1]
        if change.action in ("created", "undone"):
            raise ValidationError("没有可撤销的最近修改（已撤销时不继续撤销更早版本）")
        before = change.changes.get("_before")
        if before is None:  # 兼容前两轮已保存的简单历史。
            before = self.state()
            if change.action == "deadline_changed":
                before["deadline"] = change.changes["deadline"]["before"]
            elif change.action == "completed":
                before.update(status="pending", completed_at=None)
            else:
                raise ValidationError("旧历史缺少恢复快照，无法自动撤销")
        values = {**before, "deadline": Deadline.from_dict(before["deadline"]),
                  "submission": Submission.from_dict(before["submission"]), "review": TaskReview.from_dict(before["review"])}
        values.pop("source")  # 原通知始终保持不变。
        restored = self.mutate(values, "undone", reason=f"撤销 {change.action}，原操作时间 {change.at}")
        if restored is self:
            raise ValidationError("最近历史没有可恢复的字段变化")
        return restored

    def to_dict(self) -> dict:
        result = asdict(self)
        result["history"] = list(result["history"])
        result["review"]["uncertain_fields"] = list(result["review"]["uncertain_fields"])
        result["review"]["notes"] = list(result["review"]["notes"])
        return result

    @classmethod
    def from_dict(cls, value: Any) -> Task:
        if isinstance(value, dict) and "deleted_at" not in value:
            value = {**value, "deleted_at": None}
        if isinstance(value, dict) and "review" not in value:
            value = {**value, "review": {"needs_confirmation": False, "uncertain_fields": [], "notes": []}}
        exact_keys(value, set(cls.__dataclass_fields__), "task")
        if not isinstance(value["history"], list):
            raise ValidationError("history 必须是数组")
        return cls(**{**value, "deadline": Deadline.from_dict(value["deadline"]),
                      "submission": Submission.from_dict(value["submission"]), "source": Notice.from_dict(value["source"]),
                      "review": TaskReview.from_dict(value["review"]),
                      "history": tuple(Change.from_dict(entry) for entry in value["history"])})
