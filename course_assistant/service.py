"""应用服务：验证台账，在同一写锁内读、改、原子保存。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Sequence
from uuid import UUID, uuid4

from .errors import StorageError, TaskNotFoundError, ValidationError
from .models import Deadline, Notice, Submission, Task, aware_datetime, exact_keys, nonempty, parse_offset, timestamp
from .storage import DATA_FILES, JsonStore
from .interfaces import TaskDraft
from .preferences import DEFAULT_REMINDERS, validate_preferences
from copy import deepcopy

DEFAULT_CONFIG = {
    "schema_version": 1,
    "timezone_name": "Asia/Shanghai",
    "utc_offset": "+08:00",
    "backup_keep": 20,
    "ai_enabled": True,
    "reminders_enabled": False,
    "reminder_preferences": DEFAULT_REMINDERS,
}
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _version(document: dict) -> None:
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValidationError("仅支持 schema_version=1；未知版本需要显式迁移")


def validate_config(document: Any) -> dict:
    if isinstance(document, dict) and "reminder_preferences" not in document:
        document = {**document, "reminder_preferences": deepcopy(DEFAULT_REMINDERS)}
    exact_keys(document, set(DEFAULT_CONFIG), "config")
    _version(document)
    nonempty(document["timezone_name"], "timezone_name")
    parse_offset(document["utc_offset"])
    if type(document["backup_keep"]) is not int or not 1 <= document["backup_keep"] <= 1000:
        raise ValidationError("backup_keep 必须为 1～1000 的整数")
    if type(document["ai_enabled"]) is not bool:
        raise ValidationError("ai_enabled 必须是布尔值")
    if type(document["reminders_enabled"]) is not bool:
        raise ValidationError("reminders_enabled 必须为布尔值")
    return {**document, "reminder_preferences": validate_preferences(document["reminder_preferences"])}


def validate_tasks(document: Any) -> list[Task]:
    keys = {"schema_version", "tasks"}
    if isinstance(document, dict) and "processed_batches" in document:
        keys.add("processed_batches")
    exact_keys(document, keys, "deadlines")
    _version(document)
    if not isinstance(document["tasks"], list):
        raise ValidationError("tasks 必须是数组")
    tasks = [Task.from_dict(value) for value in document["tasks"]]
    if len({task.id for task in tasks}) != len(tasks):
        raise ValidationError("台账中存在重复任务 ID")
    receipts = document.get("processed_batches", [])
    if not isinstance(receipts, list):
        raise ValidationError("processed_batches 必须为数组")
    ids = set()
    for receipt in receipts:
        exact_keys(receipt, {"id", "at", "task_ids", "outcomes"}, "processed batch")
        key = receipt["id"]
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key) or key in ids:
            raise ValidationError("处理记录 ID 不合法或重复")
        ids.add(key)
        aware_datetime(receipt["at"], "processed batch.at")
        if not isinstance(receipt["task_ids"], list) or any(not isinstance(v, str) or v not in {t.id for t in tasks} for v in receipt["task_ids"]):
            raise ValidationError("处理记录引用未知任务")
        if not isinstance(receipt["outcomes"], list) or any(v not in ("new", "duplicate", "update") for v in receipt["outcomes"]):
            raise ValidationError("处理记录 outcome 不合法")
    return tasks


def validate_courses(document: Any) -> list[dict]:
    exact_keys(document, {"schema_version", "courses"}, "courses")
    _version(document)
    if not isinstance(document["courses"], list):
        raise ValidationError("courses 必须是数组")
    ids, names = set(), set()
    for course in document["courses"]:
        exact_keys(course, {"id", "name", "created_at"}, "course")
        try:
            if str(UUID(course["id"])) != course["id"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("课程 ID 必须是标准 UUID") from exc
        if nonempty(course["name"], "course.name") != course["name"]:
            raise ValidationError("课程名不能含首尾空白")
        aware_datetime(course["created_at"], "course.created_at")
        if course["id"] in ids or course["name"].casefold() in names:
            raise ValidationError("存在重复课程 ID 或课程名")
        ids.add(course["id"])
        names.add(course["name"].casefold())
    return [dict(course) for course in document["courses"]]


VALIDATORS = {"deadlines.json": validate_tasks, "courses.json": validate_courses, "config.json": validate_config}


@dataclass(frozen=True)
class UpdateResult:
    task: Task
    changed: bool


class TaskService:
    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR, *, lock_timeout: float = 5.0):
        self.store = JsonStore(data_dir, lock_timeout=lock_timeout)

    def _read(self, name: str) -> Any:
        try:
            return VALIDATORS[name](self.store.read(name))
        except ValidationError as exc:
            raise StorageError(f"{self.store.root / name} 校验失败：{exc}；原文件未被覆盖。") from exc

    def initialize(self) -> list[str]:
        defaults = {"deadlines.json": {"schema_version": 1, "tasks": []},
                    "courses.json": {"schema_version": 1, "courses": []}, "config.json": DEFAULT_CONFIG}
        with self.store.lock():
            # 先检查全部已有文件，防止初始化掩盖损坏或未知版本。
            for name in DATA_FILES:
                if (self.store.root / name).exists():
                    self._read(name)
            created = []
            for name in DATA_FILES:
                if not (self.store.root / name).exists():
                    self.store.write(name, defaults[name])
                    created.append(name)
            if created:
                self.store.audit("initialized", details={"files": created})
            return created

    def config(self) -> dict:
        return self._read("config.json")

    def add(self, *, course: str, title: str, description: str = "", deadline: Deadline | None = None,
            submission: Submission | None = None, source: Notice | None = None) -> Task:
        with self.store.lock():
            config = self.config()
            tasks = self._read("deadlines.json")
            task = Task.create(course=course, title=title, description=description,
                               deadline=deadline if deadline is not None else Deadline(utc_offset=config["utc_offset"]),
                               submission=submission, source=source)
            self._save_tasks([*tasks, task], config)
            self.store.audit("task_added", task_id=task.id)
            return task

    def document(self) -> dict:
        document = self.store.read("deadlines.json")
        try:
            validate_tasks(document)
        except ValidationError as exc:
            raise StorageError(f"任务台账校验失败：{exc}；原文件未被覆盖。") from exc
        return document

    def _save_tasks(self, tasks: list[Task], config: dict, *, receipts: list | None = None) -> None:
        document = {"schema_version": 1, "tasks": [task.to_dict() for task in tasks]}
        previous = self.document().get("processed_batches", []) if receipts is None else receipts
        if previous:
            document["processed_batches"] = previous
        validate_tasks(document)
        self.store.write("deadlines.json", document, keep=config["backup_keep"])

    def import_drafts(self, drafts: Sequence[TaskDraft], *, confirmed: bool = False) -> list[Task]:
        """只接收已展示并获人工确认的批次；一次原子保存，避免部分导入。"""
        if confirmed is not True:
            raise ValidationError("解析结果必须经用户确认后才能导入")
        created = []
        for draft in drafts:
            if not isinstance(draft, TaskDraft):
                raise ValidationError("批次中存在非法候选任务")
            created.append(Task.create(course=draft.course, title=draft.title, description=draft.description,
                                       deadline=draft.deadline, submission=draft.submission, source=draft.source, review=draft.review))
        if not created:
            return []
        with self.store.lock():
            config = self.config()
            if not config["ai_enabled"]:
                raise ValidationError("config.json 中 ai_enabled 已关闭")
            tasks = self._read("deadlines.json")
            # 兼容第2轮入口，但不再允许相同候选重复新增；复杂关系须走通知计划。
            from .matching import exact_duplicate
            unique = []
            for task in created:
                if not exact_duplicate(task, [*tasks, *unique]):
                    unique.append(task)
            created = unique
            if not created:
                return []
            self._save_tasks([*tasks, *created], config)
            self.store.audit("ai_import_confirmed", details={"task_ids": [task.id for task in created], "count": len(created)})
        return created

    def list(self, *, status: str = "all", course: str | None = None, query: str | None = None,
             sort: str = "deadline", descending: bool = False, include_deleted: bool = False) -> list[Task]:
        if status not in ("all", "pending", "completed"):
            raise ValidationError("查询状态必须为 all/pending/completed")
        if sort not in ("deadline", "course", "created"):
            raise ValidationError("排序字段必须为 deadline/course/created")
        tasks = self._read("deadlines.json")
        tasks = [task for task in tasks if (include_deleted or task.deleted_at is None) and (status == "all" or task.status == status)
                 and (course is None or task.course == course)
                 and (query is None or query.casefold() in f"{task.course}\n{task.title}\n{task.description}".casefold())]
        if sort == "deadline":
            # 未知日期无论正序倒序都放最后；同一截止时间以创建时间/ID 稳定排序。
            known = [task for task in tasks if task.deadline.date is not None]
            unknown = [task for task in tasks if task.deadline.date is None]
            return sorted(known, key=lambda task: (task.deadline.sort_key(), task.created_at, task.id), reverse=descending) + sorted(unknown, key=lambda task: (task.created_at, task.id), reverse=descending)
        if sort == "course":
            return sorted(tasks, key=lambda task: (task.course.casefold(), task.title.casefold(), task.id), reverse=descending)
        return sorted(tasks, key=lambda task: (aware_datetime(task.created_at, "created_at"), task.id), reverse=descending)

    def get(self, task_id: str) -> Task:
        for task in self._read("deadlines.json"):
            if task.id == task_id:
                return task
        raise TaskNotFoundError(f"找不到任务：{task_id}（请使用完整 ID）")

    def _update(self, task_id: str, *, deadline: Deadline | None = None, complete: bool = False, expected_version: str | None = None) -> UpdateResult:
        with self.store.lock():
            config = self.config()
            tasks = self._read("deadlines.json")
            for index, current in enumerate(tasks):
                if current.id == task_id:
                    if expected_version is not None and current.updated_at != expected_version:
                        raise ValidationError("任务已变化，请重新预览确认")
                    updated = current.complete() if complete else current.set_deadline(deadline)
                    if updated == current:
                        return UpdateResult(current, False)
                    tasks[index] = updated
                    self._save_tasks(tasks, config)
                    self.store.audit("task_completed" if complete else "deadline_changed", task_id=task_id)
                    return UpdateResult(updated, True)
            raise TaskNotFoundError(f"找不到任务：{task_id}（请使用完整 ID）")

    def complete(self, task_id: str) -> UpdateResult:
        return self._update(task_id, complete=True)

    def set_completed(self, task_id: str, completed: bool, *, expected_version: str | None = None) -> UpdateResult:
        """界面状态切换；恢复待办仍保留历史，并防止旧页面覆盖其他修改。"""
        if type(completed) is not bool:
            raise ValidationError("completed 必须为布尔值")
        with self.store.lock():
            config = self.config()
            tasks = self._read("deadlines.json")
            for index, current in enumerate(tasks):
                if current.id != task_id:
                    continue
                if expected_version is not None and current.updated_at != expected_version:
                    raise ValidationError("任务已变化，请刷新页面后重试")
                if current.deleted_at is not None:
                    raise ValidationError("已删除任务不能切换状态，请先撤销删除")
                updated = current.complete() if completed else current.mutate(
                    {"status": "pending", "completed_at": None}, "task_updated", reason="用户重新标记未完成")
                if updated == current:
                    return UpdateResult(current, False)
                tasks[index] = updated
                self._save_tasks(tasks, config)
                self.store.audit("task_completed" if completed else "task_reopened", task_id=task_id)
                return UpdateResult(updated, True)
            raise TaskNotFoundError(f"找不到任务：{task_id}")

    def set_deadline(self, task_id: str, deadline: Deadline, *, expected_version: str | None = None) -> UpdateResult:
        if not isinstance(deadline, Deadline):
            raise ValidationError("deadline 必须是 Deadline")
        return self._update(task_id, deadline=deadline, expected_version=expected_version)

    def apply_default_deadline_time(self, task_id: str, *, clock: str = "23:59", confirmed: bool = False,
                                    expected_version: str | None = None) -> UpdateResult:
        """用户明确采用日期末规则后更新已有任务；保留快照、原通知和撤销能力。"""
        from .deadline_policy import date_has_uncertainty, review_with_default_clock, validate_default_time, with_default_clock
        if confirmed is not True:
            raise ValidationError("默认截止时刻应用到已有任务前必须获得用户确认")
        validate_default_time(clock)
        with self.store.lock():
            config = self.config()
            tasks = self._read("deadlines.json")
            for index, task in enumerate(tasks):
                if task.id != task_id:
                    continue
                if expected_version is not None and task.updated_at != expected_version:
                    raise ValidationError("任务已变化，请重新预览确认")
                if task.deleted_at is not None or task.status == "completed":
                    raise ValidationError("已完成或删除的任务不应用默认截止时刻")
                if task.deadline.precision != "date":
                    return UpdateResult(task, False)
                if date_has_uncertainty(task.review.notes):
                    raise ValidationError("日期仍存在歧义，不能只用默认钟点解除待确认")
                updated = task.mutate({"deadline": with_default_clock(task.deadline, clock),
                                       "review": review_with_default_clock(task.review, clock)}, "deadline_changed",
                                      reason=f"用户明确要求：只有日期时默认当天{clock}截止；个人约定，非老师明示")
                tasks[index] = updated
                self._save_tasks(tasks, config)
                self.store.audit("default_deadline_time_applied", task_id=task.id, details={"clock": clock, "date": task.deadline.date})
                return UpdateResult(updated, True)
            raise TaskNotFoundError(f"找不到任务：{task_id}")

    def recoverable_action(self, task_id: str, *, action: str, confirmed: bool = False, expected_version: str | None = None) -> Task:
        if confirmed is not True or action not in ("delete", "undo"):
            raise ValidationError("删除/撤销必须先展示差异并人工确认")
        with self.store.lock():
            config = self.config()
            tasks = self._read("deadlines.json")
            for index, current in enumerate(tasks):
                if current.id != task_id:
                    continue
                if expected_version is not None and current.updated_at != expected_version:
                    raise ValidationError("任务已变化，请重新预览确认")
                changed = current.undo() if action == "undo" else current.mutate({"deleted_at": timestamp()}, "task_deleted", reason="用户确认软删除")
                if action == "delete" and current.deleted_at is not None:
                    raise ValidationError("任务已删除")
                tasks[index] = changed
                self._save_tasks(tasks, config)
                self.store.audit("task_undone" if action == "undo" else "task_deleted", task_id=task_id)
                return changed
            raise TaskNotFoundError(f"找不到任务：{task_id}")

    def courses(self) -> list[dict]:
        return sorted(self._read("courses.json"), key=lambda course: course["name"].casefold())

    def add_course(self, name: str) -> dict:
        name = nonempty(name, "课程名")
        with self.store.lock():
            config = self.config()
            courses = self._read("courses.json")
            if any(course["name"].casefold() == name.casefold() for course in courses):
                raise ValidationError(f"课程已存在：{name}")
            course = {"id": str(uuid4()), "name": name, "created_at": timestamp()}
            document = {"schema_version": 1, "courses": [*courses, course]}
            validate_courses(document)
            self.store.write("courses.json", document, keep=config["backup_keep"])
            self.store.audit("course_added", details={"course_id": course["id"]})
            return course

    def backup(self) -> list[Path]:
        with self.store.lock():
            config = self.config()
            for name in DATA_FILES:
                self._read(name)
            if (self.store.root / "reminders.json").exists():
                from .reminders import validate_state
                validate_state(self.store.read("reminders.json"))
            paths = self.store.snapshot(keep=config["backup_keep"])
            self.store.audit("manual_backup", details={"files": [str(path.relative_to(self.store.root)) for path in paths]})
            return paths

    def update_preferences(self, *, enabled: bool | None = None, lead_days: list[int] | None = None,
                           check_time: str | None = None, channel: str | None = None) -> dict:
        with self.store.lock():
            config = self.config()
            if enabled is not None: config["reminders_enabled"] = enabled
            prefs = config["reminder_preferences"]
            if lead_days is not None: prefs["lead_days"] = lead_days
            if check_time is not None: prefs["daily_check_time"] = check_time
            if channel is not None: prefs["channel"] = channel
            config = validate_config(config)
            self.store.write("config.json", config, keep=config["backup_keep"])
            self.store.audit("reminder_preferences_changed", details={"enabled": config["reminders_enabled"], "daily_check_time": prefs["daily_check_time"]})
            return config
