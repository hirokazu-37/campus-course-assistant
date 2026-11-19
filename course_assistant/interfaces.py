"""组件边界；第4轮提醒使用 reminders.py 的计划与执行结果类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol, Sequence

from .models import Deadline, Notice, Submission, Task, TaskReview


@dataclass(frozen=True)
class TaskDraft:
    """候选没有 ID/状态/历史，经关系计划和人工确认后由 Python 服务保存。"""

    course: str
    title: str
    description: str
    deadline: Deadline
    submission: Submission
    source: Notice
    review: TaskReview = TaskReview()


@dataclass(frozen=True)
class ParseContext:
    reference_time: date | datetime | None
    utc_offset: str
    course_names: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateDecision:
    kind: Literal["new", "duplicate", "possible_update", "uncertain"]
    related_task_ids: tuple[str, ...]
    reason: str
    requires_confirmation: bool = True


@dataclass(frozen=True)
class ReminderPreferences:
    utc_offset: str
    lead_minutes: tuple[int, ...]
    quiet_start: str | None = None
    quiet_end: str | None = None


@dataclass(frozen=True)
class ReminderSuggestion:
    task_id: str
    planned_at: datetime
    reason: str


class NotificationParser(Protocol):
    def parse(self, notice: Notice, context: ParseContext) -> Sequence[TaskDraft]: ...


class DuplicateDetector(Protocol):
    def assess(self, candidate: TaskDraft, existing: Sequence[Task]) -> DuplicateDecision: ...


class ReminderPlanner(Protocol):
    def plan(self, tasks: Sequence[Task], now: datetime, preferences: ReminderPreferences) -> Sequence[ReminderSuggestion]: ...
