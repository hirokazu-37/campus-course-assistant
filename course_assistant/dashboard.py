"""界面的薄适配层：复用服务、AI 缓存、通知计划和纯提醒算法，无第二套存储。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .cache import CachedClient
from .errors import ValidationError
from .interfaces import ParseContext
from .llm import ApiSettings, StructuredClient
from .matching import RELATION_SCHEMA, SemanticMatcher
from .message_time import MESSAGE_TIME_SCHEMA, parse_sent_at, resolve_message_time
from .models import Notice, Task, notice_time, parse_offset
from .parsing import AINotificationParser, ParserSettings
from .reminders import DeadlineReminderPlanner, ReminderPlan, deadline_confirmed, due_at, read_state, upcoming
from .schema import PARSE_SCHEMA
from .workflow import NoticeWorkflow

SHANGHAI = parse_offset("+08:00")


def build_workflow(service, api_settings: ApiSettings | None = None) -> NoticeWorkflow:
    settings = ParserSettings.load()
    parser = AINotificationParser(CachedClient(service.store.root,
        lambda: StructuredClient(api_settings or ApiSettings.from_env()), PARSE_SCHEMA, stage="parse"), settings)
    relations = CachedClient(service.store.root,
        lambda: StructuredClient(api_settings or ApiSettings.from_env(), schema=RELATION_SCHEMA, schema_name="notice_relation"),
        RELATION_SCHEMA, stage="relations")
    return NoticeWorkflow(service, parser, SemanticMatcher(relations, settings.aliases))


def preview_notice(service, text: str, sent_at: str, *, api_settings: ApiSettings | None = None, **options):
    if not service.config()["ai_enabled"]:
        raise ValidationError("config.json 中 AI 解析已关闭")
    if not text.strip() or len(text) > 20000:
        raise ValidationError("请粘贴1～20000字符的通知")
    # 时间抽取也可能请求模型，先检查损坏台账/无效人工选择，避免无效付费。
    document = service.document()
    target = options.get("target_id")
    if target and target not in {task["id"] for task in document["tasks"]}:
        raise ValidationError("人工选择的任务 ID 不存在；未调用模型")
    if target and options.get("as_new"):
        raise ValidationError("不能同时指定旧任务目标和新建")
    if options.get("due_override") is not None and not target and not options.get("as_new"):
        raise ValidationError("人工核实截止时间必须同时选择目标或新建；未调用模型")
    courses = sorted({course["name"] for course in service.courses()} |
                     {task.course for task in service.list() if "course" not in task.review.uncertain_fields})
    time_client = CachedClient(service.store.root,
        lambda: StructuredClient(api_settings or ApiSettings.from_env(), schema=MESSAGE_TIME_SCHEMA, schema_name="message_time"),
        MESSAGE_TIME_SCHEMA, stage="message_time")
    resolved = resolve_message_time(text, sent_at, time_client)
    notice = Notice(text, resolved.value)
    context = ParseContext(notice_time(notice.sent_at), "+08:00", tuple(courses))
    plan = build_workflow(service, api_settings).prepare(notice, context, **options)
    if resolved.warnings:
        # 随预览与任务历史保留AI时间来源，不能只在屏幕短暂显示。
        actions = tuple(replace(action, draft=replace(action.draft,
            review=replace(action.draft.review, notes=tuple(dict.fromkeys((*action.draft.review.notes, *resolved.warnings))))))
            for action in plan.actions)
        plan = replace(plan, actions=actions, warnings=tuple(dict.fromkeys((*resolved.warnings, *plan.warnings))))
    return plan


def save_plan(service, plan, *, confirmed: bool = False):
    # 提交时不创建 API 客户端，也不重新调用模型。
    return NoticeWorkflow(service, None, None).apply(plan, confirmed=confirmed)


def deadline_label(deadline) -> str:
    label = deadline.date or "日期待确认"
    if deadline.time:
        label += " " + deadline.time
    return label + (" · 待确认" if deadline.needs_confirmation else "")


def remaining(task: Task, now: datetime) -> str:
    if task.status == "completed":
        return "已完成"
    if not deadline_confirmed(task):
        return "待补充截止时间"
    seconds = (due_at(task) - now).total_seconds()
    if seconds == 0:
        return "现已截止"
    minutes = int(abs(seconds) // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    value = f"{days}天 {hours}小时" if days else f"{hours}小时 {minutes}分钟" if hours else f"{minutes}分钟" if minutes else "不足1分钟"
    return ("已逾期 " if seconds < 0 else "剩余 ") + value


def overview(service, now: datetime) -> dict:
    """一致快照，只计算预览；不会通知、写凭据或消费提醒节点。"""
    with service.store.lock():
        config = service.config()
        tasks = service.list()
        state = read_state(service.store)
    plan = DeadlineReminderPlanner().plan(tasks, now, config["reminder_preferences"], state["receipts"]) if config["reminders_enabled"] else ReminderPlan((), ())
    return {"config": config, "tasks": tasks, "state": state, "reminder_plan": plan, "agenda": upcoming(tasks, now)}
