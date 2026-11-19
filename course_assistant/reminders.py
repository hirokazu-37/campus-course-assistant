"""纯时间计划 + 可注入时钟的执行器；无 AI、无系统计划任务注册。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from .cache import digest
from .errors import StorageError, ValidationError
from .models import Task, aware_datetime, exact_keys, parse_offset, timestamp
from .notifications import Delivery, Notifier


def empty_state() -> dict:
    return {"schema_version": 1, "receipts": {}, "last_check_at": None, "last_run": None}


def validate_state(value: dict) -> dict:
    try:
        exact_keys(value, {"schema_version", "receipts", "last_check_at", "last_run"}, "reminders")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["receipts"], dict):
            raise ValidationError("不支持的提醒状态结构")
        if value["last_check_at"] is not None: aware_datetime(value["last_check_at"], "last_check_at")
        if value["last_run"] is not None and not isinstance(value["last_run"], dict): raise ValidationError("last_run 必须是对象")
        for key, receipt in value["receipts"].items():
            exact_keys(receipt, {"task_id", "revision", "node", "planned_at", "recorded_at", "run_id", "status", "delivery"}, "reminder receipt")
            if not isinstance(key, str) or key != digest({"task": receipt["task_id"], "revision": receipt["revision"], "node": receipt["node"]}):
                raise ValidationError("提醒凭据键不一致")
            if any(not isinstance(receipt[name], str) or not receipt[name] for name in ("task_id", "revision", "node", "run_id")):
                raise ValidationError("提醒凭据身份字段不合法")
            if receipt["status"] not in ("reserved", "delivered", "fallback", "superseded", "unknown"):
                raise ValidationError("提醒凭据状态不合法")
            if receipt["delivery"] is not None and not isinstance(receipt["delivery"], dict): raise ValidationError("delivery 不合法")
            aware_datetime(receipt["planned_at"], "planned_at"); aware_datetime(receipt["recorded_at"], "recorded_at")
    except (ValidationError, TypeError, ValueError) as exc:
        raise StorageError(f"提醒状态校验失败：{exc}；没有重置或发送，请检查备份。") from exc
    return value


def read_state(store) -> dict:
    return validate_state(store.read("reminders.json")) if (store.root / "reminders.json").exists() else empty_state()


def deadline_confirmed(task: Task) -> bool:
    return (task.deadline.precision in ("minute", "second") and not task.deadline.needs_confirmation and
            "deadline" not in task.review.uncertain_fields)


def schedule_revision(task: Task) -> str:
    # A→B→A（撤销延期）恢复原计划，不能重复提醒原A计划已送达的节点。
    return digest({"deadline": asdict(task.deadline), "confirmed": deadline_confirmed(task)})


def due_at(task: Task) -> datetime:
    return aware_datetime(f"{task.deadline.date}T{task.deadline.time}{task.deadline.utc_offset}", "deadline")


@dataclass(frozen=True)
class ReminderEvent:
    task_id: str
    revision: str
    node: str
    planned_at: str
    title: str
    message: str
    catch_up: bool

    @property
    def key(self) -> str:
        return digest({"task": self.task_id, "revision": self.revision, "node": self.node})


@dataclass(frozen=True)
class ReminderPlan:
    events: tuple[ReminderEvent, ...]
    superseded: tuple[ReminderEvent, ...]


class DeadlineReminderPlanner:
    def plan(self, tasks: list[Task], now: datetime, preferences: dict, receipts: dict | None = None) -> ReminderPlan:
        if now.tzinfo is None or now.utcoffset() is None: raise ValidationError("提醒检查时间必须包含时区")
        receipts = receipts or {}
        events, superseded = [], []
        for task in tasks:
            if task.status != "pending" or task.deleted_at is not None: continue
            override = preferences["course_overrides"].get(task.course)
            if override and not override["enabled"]: continue
            days = override["lead_days"] if override else preferences["lead_days"]
            revision = schedule_revision(task)
            def event(node, planned, message):
                return ReminderEvent(task.id, revision, node, planned.isoformat(), f"{task.course} · {task.title}", message, now > planned)
            if not deadline_confirmed(task):
                item = event("needs_deadline", now, f"[{task.course}] {task.title}：截止日期/钟点仍待确认，请补充完整时间；不生成精确倒计时。")
                if item.key not in receipts: events.append(item)
                continue
            due = due_at(task)
            leads = [event(f"lead:{day}", due - timedelta(days=day),
                           f"[{task.course}] {task.title}：提前{day}天节点；截止 {due.isoformat()}。") for day in days]
            overdue = event("overdue", due, f"[{task.course}] {task.title} 已超过确认的截止时间 {due.isoformat()}，请核实是否已提交或需要延期。")
            if now > due:
                if overdue.key not in receipts:
                    events.append(overdue)
                    superseded.extend(item for item in leads if item.key not in receipts)
                continue
            # 曾发过逾期节点后，不因系统时钟回拨重新发送较早节点。
            if overdue.key in receipts: continue
            reached = sorted((item for item in leads if aware_datetime(item.planned_at, "planned_at") <= now), key=lambda item: item.planned_at)
            if not reached: continue
            latest = reached[-1]
            if latest.key not in receipts:
                if latest.catch_up:
                    latest = ReminderEvent(latest.task_id, latest.revision, latest.node, latest.planned_at, latest.title,
                                           latest.message + f" 补检：原计划 {latest.planned_at}，本次检查 {now.isoformat()}。", True)
                events.append(latest)
                superseded.extend(item for item in reached[:-1] if item.key not in receipts)
        return ReminderPlan(tuple(events), tuple(superseded))


def upcoming(tasks: list[Task], now: datetime, *, days: int = 7, course: str | None = None) -> dict:
    if now.tzinfo is None or now.utcoffset() is None or type(days) is not int or not 1 <= days <= 365:
        raise ValidationError("未来任务窗口必须为1～365天，查询时间须包含时区")
    end = now + timedelta(days=days)
    selected, uncertain = [], []
    for task in tasks:
        if task.status != "pending" or task.deleted_at is not None or (course is not None and task.course != course): continue
        if deadline_confirmed(task):
            if now <= due_at(task) < end: selected.append(task)
        elif task.deadline.date is None or now.date().isoformat() <= task.deadline.date < end.date().isoformat():
            uncertain.append(task)
    return {"window_start": now.isoformat(), "window_end_exclusive": end.isoformat(),
            "tasks": [task.to_dict() for task in sorted(selected, key=due_at)],
            "needs_information": [task.to_dict() for task in uncertain]}


class RunLog:
    def __init__(self, root: Path, *, mode: str, now: datetime, simulated: bool, dry_run: bool):
        self.run_id = uuid4().hex
        folder = root / "logs" / "reminder-runs"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{self.run_id}.jsonl"
        self.context = {"run_id": self.run_id, "trigger": mode, "effective_now": now.isoformat(), "simulated": simulated, "dry_run": dry_run}

    def write(self, event: str, **details) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({**self.context, "observed_at": timestamp(), "event": event, **details}, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush(); os.fsync(handle.fileno())


class ReminderRunner:
    def __init__(self, service, notifier=None): self.service, self.notifier = service, notifier

    def check(self, *, now: datetime | None = None, dry_run: bool = False, channel: str | None = None, trigger: str = "manual") -> dict:
        simulated = now is not None
        effective = now if now is not None else datetime.now(timezone.utc)
        if effective.tzinfo is None or effective.utcoffset() is None: raise ValidationError("检查时间必须包含时区")
        log = RunLog(self.service.store.root, mode=trigger, now=effective, simulated=simulated, dry_run=dry_run)
        log.write("run_started")
        try:
            self.service.initialize()
            with self.service.store.lock():
                config = self.service.config()
                state = read_state(self.service.store)
                tasks = self.service._read("deadlines.json")
                plan = DeadlineReminderPlanner().plan(tasks, effective, config["reminder_preferences"], state["receipts"]) if config["reminders_enabled"] else ReminderPlan((), ())
                log.write("plan_created", enabled=config["reminders_enabled"], events=[asdict(event) for event in plan.events],
                          superseded=[event.key for event in plan.superseded])
                if not dry_run:
                    for item in (*plan.events, *plan.superseded):
                        state["receipts"][item.key] = {"task_id": item.task_id, "revision": item.revision, "node": item.node,
                            "planned_at": item.planned_at, "recorded_at": timestamp(), "run_id": log.run_id,
                            "status": "superseded" if item in plan.superseded else "reserved", "delivery": None}
                    previous = aware_datetime(state["last_check_at"], "last_check_at") if state["last_check_at"] else None
                    state["last_check_at"] = max(previous, effective).isoformat() if previous else effective.isoformat()
                    state["last_run"] = {"run_id": log.run_id, "status": "running", "log": str(log.path), "trigger": trigger}
                    self._save(state, config)
            delivery = None
            if plan.events and not dry_run:
                log.write("delivery_start", event_keys=[item.key for item in plan.events])
                # 每次检查汇总成一个桌面通知，完整明细保留在终端和日志。
                message = "\n".join(item.message for item in plan.events)
                desktop_message = message if len(message) <= 550 else message[:450] + f"\n共{len(plan.events)}项，完整明细见本次运行日志。"
                for item in plan.events: print(item.message)
                delivery = (self.notifier or Notifier(channel or config["reminder_preferences"]["channel"])).send(f"课程任务提醒 · {len(plan.events)}项", desktop_message)
                log.write("delivery_result", delivery=asdict(delivery))
            if not dry_run:
                with self.service.store.lock():
                    state = read_state(self.service.store)
                    for item in plan.events:
                        receipt = state["receipts"][item.key]
                        if receipt["run_id"] == log.run_id:
                            receipt["status"] = "delivered" if delivery.channel == "windows_desktop" else "fallback"
                            receipt["delivery"] = asdict(delivery)
                    state["last_run"] = {"run_id": log.run_id, "status": "finished", "log": str(log.path), "trigger": trigger,
                                         "finished_at": timestamp(), "count": len(plan.events)}
                    self._save(state, config)
            result = {"run_id": log.run_id, "log": str(log.path), "enabled": config["reminders_enabled"], "dry_run": dry_run,
                      "simulated": simulated, "count": len(plan.events), "events": [asdict(item) for item in plan.events],
                      "superseded_count": len(plan.superseded), "delivery": asdict(delivery) if delivery else None,
                      "delivery_unknown_count": sum(value["status"] in ("reserved", "unknown") for value in state["receipts"].values())}
            if result["delivery_unknown_count"]:
                log.write("unconfirmed_deliveries", count=result["delivery_unknown_count"])
                print("存在送达状态不明的历史节点，请运行 remind status 核对；为避免重复，不自动重发。")
            log.write("run_finished", count=len(plan.events))
            return result
        except Exception as exc:
            log.write("run_failed", error_type=type(exc).__name__, message=str(exc))
            # 预留后的中断/失败不自动重发，以免同一节点多次弹窗；status 可检查 reserved。
            raise

    def _save(self, state: dict, config: dict) -> None:
        validate_state(state)
        self.service.store.write("reminders.json", state, keep=config["backup_keep"])

    def status(self) -> dict:
        with self.service.store.lock():
            state = read_state(self.service.store)
            return {"last_check_at": state["last_check_at"], "last_run": state["last_run"], "receipt_count": len(state["receipts"]),
                    "delivery_unknown": [{"event_key": key, **value} for key, value in state["receipts"].items() if value["status"] in ("reserved", "unknown")]}

    def test_notification(self, *, channel: str = "auto") -> dict:
        now = datetime.now(timezone.utc)
        log = RunLog(self.service.store.root, mode="os_notification_test", now=now, simulated=False, dry_run=False)
        log.write("run_started")
        try:
            delivery = (self.notifier or Notifier(channel)).send("校园课程管家 · 通知测试", f"这是操作系统通知测试，不是真实作业提醒。发送时间：{now.astimezone(parse_offset('+08:00')).isoformat()}")
            result = {"log": str(log.path), **asdict(delivery), "user_visible": None}
            log.write("os_probe_result", **result)
            log.write("run_finished")
            return result
        except Exception as exc:
            log.write("run_failed", error_type=type(exc).__name__, message=str(exc))
            raise
