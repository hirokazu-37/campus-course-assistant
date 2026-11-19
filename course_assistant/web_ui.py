"""中文 Streamlit 界面；所有业务数据通过原有服务读取和保存。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time
import json
import os
from pathlib import Path
from uuid import uuid4

import streamlit as st

from . import dashboard
from .errors import ModelError, StorageError, TaskNotFoundError, ValidationError
from .llm import ApiSettings
from .models import Deadline
from .parsing import ParserSettings
from .reminders import ReminderRunner, deadline_confirmed, due_at
from .service import DEFAULT_DATA_DIR, TaskService

PROJECT = Path(os.environ.get("COURSE_ASSISTANT_PROJECT_ROOT") or Path(__file__).resolve().parent.parent)
KNOWN_ERRORS = (ModelError, StorageError, TaskNotFoundError, ValidationError, OSError)
FIELD_LABELS = {"course": "课程", "title": "作业名称", "description": "任务内容", "deadline": "截止时间",
                "submission": "提交要求", "submission.method": "提交方式", "submission.format_requirements": "格式要求",
                "review": "待确认信息", "deleted_at": "删除状态", "status": "任务状态", "completed_at": "完成时间"}
STYLE = """
<style>
 .stApp {font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif;}
 [data-testid="stHeader"] {display:none !important;}
 .block-container {max-width:1280px;padding:1rem 2rem 2rem;}
 h1 {font-size:clamp(1.5rem,2.5vw,2rem) !important;letter-spacing:-.02em;font-weight:750;padding:0 !important;margin:0 !important;line-height:1.2 !important;}
 h2, h3 {font-size:1.25rem !important;letter-spacing:-.02em;}
 [data-testid="stMetric"] {background:#fff;border:1px solid #dce7e2;border-radius:12px;padding:12px 16px;}
 [data-testid="stMetricLabel"] {color:#61766e;}
 [data-testid="stMetricValue"] {font-size:2rem;font-weight:650;color:#166d62;}
 [data-testid="stVerticalBlockBorderWrapper"] {border-radius:14px;}
 div.stButton > button, div.stFormSubmitButton > button {border-radius:9px;}
 .st-key-main_navigation [role="radiogroup"] {background:#e6efeb;border:1px solid #d5e4dc;border-radius:12px;padding:5px;gap:5px;width:fit-content;flex-wrap:wrap;}
 .st-key-main_navigation label[data-baseweb="radio"] {margin:0;padding:8px 18px;border-radius:9px;transition:background .15s;}
 .st-key-main_navigation label[data-baseweb="radio"] > div:first-child {width:0;min-width:0;margin:0;opacity:0;}
 .st-key-main_navigation label[data-baseweb="radio"]:has(input:checked) {background:#16786f;color:white;box-shadow:0 2px 5px #183f3320;}
 .st-key-main_navigation label[data-baseweb="radio"]:has(input:focus-visible) {outline:2px solid #16786f;outline-offset:2px;}
 .st-key-main_navigation label[data-baseweb="radio"] p {color:inherit;font-weight:600;}
 .st-key-main_navigation label[data-baseweb="radio"]:has(input:checked) p {color:#fff !important;}
 [data-testid="stText"] {overflow-wrap:anywhere;}
 @media(max-width:700px) {.block-container {padding:1rem 1rem 1.5rem;} .st-key-main_navigation label[data-baseweb="radio"] {padding:7px 12px;}}
</style>
"""


def now_shanghai():
    return datetime.now(dashboard.SHANGHAI)


def flash(message: str):
    st.session_state["flash"] = message


def show_error(exc):
    # 不把 API 客户端、请求对象、环境变量或原始异常栈放进页面。
    st.error(str(exc) if isinstance(exc, KNOWN_ERRORS) else "操作失败。请刷新重试；原有台账不会被自动重置。")


def change_status(root, task, key):
    try:
        TaskService(root).set_completed(task.id, st.session_state[key], expected_version=task.updated_at)
        flash("已标记完成。" if st.session_state[key] else "已重新加入未完成列表。")
    except Exception as exc:
        st.session_state[key] = task.status == "completed"
        st.session_state["operation_error"] = str(exc) if isinstance(exc, KNOWN_ERRORS) else "状态保存失败，请刷新后重试。"


def show_diff(diff):
    rows = []
    for field, values in diff.items():
        def display(value):
            return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, tuple)) else "未设置" if value is None else str(value)
        rows.append({"修改字段": FIELD_LABELS.get(field, field), "修改前": display(values["before"]), "修改后": display(values["after"])})
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")


def task_details(service, task):
    with st.expander("查看内容、原通知与截止时间设置"):
        st.text(task.description or "尚无任务内容")
        st.text("提交方式：" + (task.submission.method or "待确认"))
        st.text("格式要求：" + (task.submission.format_requirements or "待确认"))
        if task.review.needs_confirmation:
            st.warning("待核对：" + "、".join(FIELD_LABELS.get(v, v) for v in task.review.uncertain_fields))
        for note in task.review.notes:
            st.text(note)
        st.caption("原始通知 · " + (task.source.sent_at or "发送时间未知"))
        st.text(task.source.raw_notice or "未记录原通知")
        st.caption("任务 ID：" + task.id)
        with st.expander("变更记录"):
            st.json([asdict(entry) for entry in task.history])
        key = f"due_{task.id}_{task.updated_at}"
        with st.form(key, enter_to_submit=False):
            due = st.text_input("核实后的截止时间", value=(f"{task.deadline.date}T{task.deadline.time}" if task.deadline.time else task.deadline.date or ""),
                                placeholder="YYYY-MM-DDTHH:MM", key=key + "_value")
            st.caption("请依据老师通知填写完整时间。只填日期仍保留待确认标记。")
            if st.form_submit_button("预览截止时间修改"):
                try:
                    proposed = Deadline.from_input(due.strip() or None)
                    updated = task.set_deadline(proposed)
                    if updated == task:
                        st.info("截止时间没有变化。")
                        st.session_state.pop("due_preview_" + task.id, None)
                    else:
                        st.session_state["due_preview_" + task.id] = (task.updated_at, proposed, uuid4().hex)
                except Exception as exc:
                    st.session_state.pop("due_preview_" + task.id, None)
                    show_error(exc)
        preview = st.session_state.get("due_preview_" + task.id)
        if preview:
            version, proposed, token = preview
            if version != task.updated_at:
                st.warning("任务已变化，请重新预览截止时间。")
            else:
                show_diff({"deadline": {"before": asdict(task.deadline), "after": asdict(proposed)}})
                consent = st.checkbox("我已核实以上截止时间修改", key="due_consent_" + token)
                if st.button("确认保存截止时间", key="due_save_" + token, disabled=not consent):
                    try:
                        service.set_deadline(task.id, proposed, expected_version=version)
                        st.session_state.pop("due_preview_" + task.id, None)
                        flash("截止时间已保存，提醒计划将按新时间计算。")
                        st.rerun()
                    except KNOWN_ERRORS as exc:
                        show_error(exc)


def render_overview(service, snapshot, now):
    tasks = snapshot["tasks"]
    pending = [task for task in tasks if task.status == "pending"]
    uncertain = [task for task in pending if not deadline_confirmed(task)]
    overdue = [task for task in pending if deadline_confirmed(task) and due_at(task) < now]
    a, b, c = st.columns(3)
    a.metric("未完成作业", len(pending))
    b.metric("未来 7 天截止", len(snapshot["agenda"]["tasks"]))
    c.metric("截止时间待确认", len(uncertain))
    st.subheader("临期与提醒")
    with st.container(border=True):
        if overdue:
            st.warning(f"有 {len(overdue)} 项已逾期，请核实是否已提交。")
        if snapshot["agenda"]["tasks"]:
            st.dataframe([{"课程": item["course"], "作业": item["title"], "截止": item["deadline"]["date"] + " " + item["deadline"]["time"]}
                          for item in snapshot["agenda"]["tasks"]], hide_index=True, width="stretch")
        elif not overdue:
            st.info("未来 7 天内暂无已确认截止时间的未完成作业。")
        if uncertain:
            st.warning(f"{len(uncertain)} 项缺少确认的完整截止时间，请在任务详情中补充。")
        config, state = snapshot["config"], snapshot["state"]
        st.caption("提醒已开启" if config["reminders_enabled"] else "提醒已关闭，可在个人设置中开启")
        last_check = datetime.fromisoformat(state["last_check_at"]).astimezone(dashboard.SHANGHAI).strftime("%Y-%m-%d %H:%M:%S") if state["last_check_at"] else "尚无记录"
        st.caption(f"当前待发送提醒节点：{len(snapshot['reminder_plan'].events)} · 上次检查：{last_check}（上海时间）")
        unknown = sum(r["status"] in ("reserved", "unknown") for r in state["receipts"].values())
        if unknown:
            st.warning(f"有 {unknown} 个历史提醒节点送达状态不明，请核对运行日志。程序不会自动重复发送。")
        st.caption("浏览页面不会发送通知。以下按钮会执行真实提醒检查并记录已发送节点。")
        if st.button("执行一次提醒检查", key="check_reminders"):
            try:
                result = ReminderRunner(service).check(trigger="streamlit")
                st.session_state["reminder_result"] = result
                flash(f"检查完成，本次处理 {result['count']} 个提醒节点。")
                st.rerun()
            except KNOWN_ERRORS as exc:
                show_error(exc)
        result = st.session_state.get("reminder_result")
        if result:
            if result["delivery"]:
                delivery = result["delivery"]
                st.caption("本次渠道：" + delivery["channel"] + "。接口成功不代表用户一定看到通知。")
                if delivery.get("fallback_reason"):
                    st.text(delivery["fallback_reason"])
            for event in result["events"]:
                st.text(event["message"])
            st.caption("运行日志：" + result["log"])
    st.subheader("我的作业")
    left, middle, right = st.columns([1, 1, 1.4])
    status = left.selectbox("任务状态", ["未完成", "已完成", "全部"], key="task_filter")
    courses = sorted({task.course for task in tasks})
    course = middle.selectbox("按课程筛选", [None, *courses], format_func=lambda value: "全部课程" if value is None else value)
    ordering = right.selectbox("排序与范围", ["截止日期从近到远", "课程名称", "创建时间", "未来7天"])
    query = st.text_input("搜索课程或作业", placeholder="输入课程名、作业名或内容")
    selected = service.list(status={"未完成": "pending", "已完成": "completed", "全部": "all"}[status], course=course,
                            query=query or None, sort={"课程名称": "course", "创建时间": "created"}.get(ordering, "deadline"))
    if ordering == "未来7天":
        ids = {item["id"] for item in snapshot["agenda"]["tasks"]}
        selected = [task for task in selected if task.id in ids]
    st.caption(f"共 {len(selected)} 项 · 时间为 Asia/Shanghai（UTC+8），刷新页面可更新剩余时间")
    if not selected:
        st.info("还没有符合条件的任务。通过顶部“通知导入”粘贴课程通知开始整理。")
    for task in selected:
        with st.container(border=True):
            title_col, due_col, state_col = st.columns([2.2, 1.5, .8])
            with title_col:
                st.caption(task.course)
                st.text(task.title)
            with due_col:
                st.caption(dashboard.deadline_label(task.deadline))
                st.text(dashboard.remaining(task, now))
            with state_col:
                key = f"status_{task.id}_{task.updated_at}"
                st.toggle("已完成", value=task.status == "completed", key=key, on_change=change_status,
                          args=(service.store.root, task, key))
            task_details(service, task)


def store_preview(plan, text, sent_at):
    st.session_state["notice_preview"] = {"plan": plan, "text": text, "sent_at": sent_at, "token": uuid4().hex}


def commit_notice(root, plan):
    """在页面重绘前提交，避免旧动态表单留在同一次渲染树中。"""
    try:
        result = dashboard.save_plan(TaskService(root), plan, confirmed=True)
        st.session_state.pop("notice_preview", None)
        flash(f"保存完成：新增 {len(result['created'])} 项，更新 {len(result['updated'])} 项，跳过重复 {result['duplicates']} 项。")
    except Exception as exc:
        st.session_state["operation_error"] = (str(exc) if isinstance(exc, KNOWN_ERRORS) else "保存失败。") + " 请重新点击“AI 解析并预览”。"


def cancel_notice():
    st.session_state.pop("notice_preview", None)
    flash("已取消，台账未修改。解析缓存保留，以便下次复用。")


def render_plan(service):
    saved = st.session_state.get("notice_preview")
    if not saved:
        return
    plan, token = saved["plan"], saved["token"]
    st.subheader("解析结果 · 尚未保存")
    with st.expander("本次预览对应的原通知与发送时间"):
        st.text(saved["text"])
        st.text("填写的发送时间说明：" + (saved["sent_at"] or "未填写"))
        resolved = plan.actions[0].draft.source.sent_at if plan.actions else None
        if resolved:
            st.text(("采用的发送日期：" if len(resolved) == 10 else "采用的发送时间：") + resolved +
                    ("（具体时刻未知，未补成零点）" if len(resolved) == 10 else "（上海时间）"))
        elif not plan.already_processed:
            st.text("发送时间未知；缺少基准的相对截止日期继续待确认。")
    if plan.already_processed:
        st.info("该批通知已经处理；未再次调用模型，也没有重复导入。")
    elif not plan.actions:
        st.info("未识别到课程任务，没有写入台账。")
    for warning in plan.warnings:
        st.warning(warning)
    current = {task.id: task for task in service.list(include_deleted=True)}
    for index, action in enumerate(plan.actions):
        with st.container(border=True):
            preview = action.preview()
            st.markdown(f"**{index + 1}. {preview['classification']}**")
            draft = action.draft
            st.text(f"{draft.course} · {draft.title}")
            st.text(draft.description)
            st.text("截止：" + dashboard.deadline_label(draft.deadline))
            st.text("提交方式：" + (draft.submission.method or "待确认") + " ｜ 格式：" + (draft.submission.format_requirements or "待确认"))
            st.caption("匹配依据")
            st.text(action.reason)
            if action.evidence:
                st.text(action.evidence)
            for target_id in action.target_ids:
                old = current.get(target_id)
                if old:
                    st.text(f"候选旧任务：{old.course} · {old.title} · {dashboard.deadline_label(old.deadline)} · {old.id}")
            if preview["change_types"]:
                st.info("、".join(preview["change_types"]))
            show_diff(action.diff)
            for issue in action.issues:
                st.warning(issue)
            if draft.review.needs_confirmation:
                st.warning("仍待核对：" + "、".join(FIELD_LABELS.get(v, v) for v in draft.review.uncertain_fields))
            for note in draft.review.notes:
                st.text(note)
    blocked = any(a.kind == "uncertain" for a in plan.actions)
    if blocked:
        st.warning("匹配尚不明确，整批不会写入。可拆分通知，或在下方明确选择目标再预览。")
    if len(plan.actions) == 1 and plan.actions[0].kind != "duplicate":
        with st.expander("人工核实关系或截止时间（单任务）"):
            with st.form("resolve_" + token, enter_to_submit=False):
                mode = st.selectbox("这条通知应如何处理", ["请选择", "修改已有任务", "明确作为新任务"])
                ids = [task.id for task in current.values() if not task.deleted_at]
                target = st.selectbox("旧任务目标", [None, *ids], format_func=lambda value: "请选择旧任务" if value is None else f"{current[value].course} · {current[value].title} · {current[value].id[:8]}")
                due = st.text_input("人工核实后的完整截止时间（可选）", placeholder="YYYY-MM-DDTHH:MM")
                if st.form_submit_button("按人工选择重新预览"):
                    try:
                        if mode == "请选择" or (mode == "修改已有任务" and target is None):
                            raise ValidationError("请明确选择关系及对应旧任务")
                        options = {"target_id": target if mode == "修改已有任务" else None, "as_new": mode == "明确作为新任务",
                                   "due_override": Deadline.from_input(due.strip()) if due.strip() else None}
                        plan = dashboard.preview_notice(service, saved["text"], saved["sent_at"], api_settings=st.session_state.get("api_settings"), **options)
                        store_preview(plan, saved["text"], saved["sent_at"])
                        st.rerun()
                    except KNOWN_ERRORS as exc:
                        show_error(exc)
    if plan.actions and not plan.already_processed:
        consent = st.checkbox("我已核对以上任务、修改前后差异；未明确的信息保留待确认", key="consent_" + token)
        st.button("确认并保存到任务台账", type="primary", key="save_" + token, disabled=blocked or not consent,
                  on_click=commit_notice, args=(service.store.root, plan))
    st.button("取消本次预览", key="cancel_" + token, on_click=cancel_notice)


def render_import(service):
    st.subheader("把通知变成待办")
    st.caption("只在点击解析时请求模型；确认后才写入正式台账。群聊中的文字仅作为待解析数据。")
    if st.button("读取本地 inbox.txt", key="read_inbox"):
        try:
            path = PROJECT / "inbox.txt"
            if path.stat().st_size > 80000:
                raise ValidationError("inbox.txt 超出输入大小限制")
            st.session_state["notice_text"] = path.read_text(encoding="utf-8-sig")
        except KNOWN_ERRORS as exc:
            show_error(exc)
    with st.form("notice_input", enter_to_submit=False):
        text = st.text_area("粘贴课程通知", height=190, max_chars=20000, key="notice_text", placeholder="在这里粘贴老师或学委的原始通知…")
        sent_at = st.text_input("消息发送日期或时间（选填，支持自然写法）", key="notice_sent_at",
                               placeholder="例如：2026年11月13日，或2026-11-13 14:30", max_chars=500)
        st.caption("只知道日期也能填。自然语言或通知头部的发送时间可交AI识别；没有依据时保留未知，不用粘贴时间代替。")
        submitted = st.form_submit_button("AI 解析并预览", type="primary", disabled=not service.config()["ai_enabled"])
    if submitted:
        # 新请求失败时清除旧预览，避免误保存另一条通知的结果。
        st.session_state.pop("notice_preview", None)
        try:
            with st.spinner("正在解析并与已有任务比对…"):
                plan = dashboard.preview_notice(service, text, sent_at, api_settings=st.session_state.get("api_settings"))
            store_preview(plan, text, sent_at)
        except Exception as exc:
            show_error(exc)
    render_plan(service)


def save_api_settings():
    try:
        previous = st.session_state.get("api_settings")
        key = st.session_state["api_key_input"].strip() or (previous.api_key if previous else os.environ.get("OPENAI_API_KEY", "").strip())
        settings = ApiSettings(key, st.session_state["api_model_input"].strip(),
                               st.session_state["api_base_input"].strip().rstrip("/"),
                               float(st.session_state["api_timeout_input"]), st.session_state["api_mode_input"])
        st.session_state["api_settings"] = settings
        st.session_state["api_key_input"] = ""
        flash("连接配置已保存到本次网页会话。可前往“通知导入”解析；尚未发送 API 请求。")
    except Exception as exc:
        st.session_state["operation_error"] = str(exc) if isinstance(exc, KNOWN_ERRORS) else "连接配置保存失败。"


def clear_api_settings():
    st.session_state.pop("api_settings", None)
    for key in ("api_key_input", "api_model_input", "api_base_input", "api_timeout_input", "api_mode_input"):
        st.session_state.pop(key, None)
    flash("已清除本次网页会话的连接配置。")


def render_api_settings():
    st.subheader("AI 连接")
    settings = st.session_state.get("api_settings")
    env_available = all(os.environ.get(name, "").strip() for name in ("OPENAI_API_KEY", "OPENAI_MODEL"))
    st.caption("当前使用：" + ("本次网页会话配置" if settings else "启动终端的环境变量" if env_available else "尚未配置"))
    st.caption("Key 隐藏输入，仅保存在当前网页会话内存中，不写入项目文件或日志。刷新、关闭页面或重启后可能需要重填；命令行继续使用环境变量。")
    with st.form("api_connection", enter_to_submit=False):
        st.text_input("API Key", type="password", key="api_key_input", placeholder="填入 Key；已配置时留空可沿用")
        st.text_input("模型名称", value=settings.model if settings else os.environ.get("OPENAI_MODEL") or "deepseek-flash", key="api_model_input")
        st.text_input("API 基础地址", value=settings.base_url if settings else os.environ.get("OPENAI_BASE_URL") or "https://api.deepseek.com", key="api_base_input")
        mode = settings.response_mode if settings else os.environ.get("OPENAI_RESPONSE_MODE", "json_object")
        st.selectbox("结构化输出模式", ["json_object", "json_schema"], index=1 if mode == "json_schema" else 0, key="api_mode_input",
                     format_func=lambda value: "JSON 模式（DeepSeek；仍执行本地严格校验）" if value == "json_object" else "严格 JSON Schema（服务商需支持）")
        try:
            timeout = settings.timeout if settings else float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60"))
            if not 1 <= timeout <= 300:
                timeout = 60.0
        except ValueError:
            timeout = 60.0
        st.number_input("请求超时（秒）", min_value=1.0, max_value=300.0, value=float(timeout), step=1.0, key="api_timeout_input")
        st.form_submit_button("保存本次会话连接", type="primary", on_click=save_api_settings)
    st.caption("以上预填 DeepSeek 地址与模型名可修改。保存只校验配置；点击“AI 解析并预览”才会把通知发送至所填接口并可能产生费用。")
    if settings:
        st.button("清除本次会话连接", on_click=clear_api_settings)


def render_settings(service, config):
    render_api_settings()
    st.divider()
    default_time = ParserSettings.load().date_only_deadline_time
    st.caption(f"通知只写日期时，默认当天 {default_time} 截止（个人约定）；通知明确给出的钟点优先。" if default_time else "通知只有日期时，具体截止时刻保留待确认。")
    st.subheader("让提醒适合你的节奏")
    prefs = config["reminder_preferences"]
    with st.form("preferences", enter_to_submit=False):
        enabled = st.toggle("启用任务提醒", value=config["reminders_enabled"])
        days = st.text_input("提前几天提醒", value=", ".join(str(day) for day in prefs["lead_days"]), help="0～365的整数，用逗号分隔；例如3, 1")
        clock = st.time_input("每日检查时间（上海时间）", value=time.fromisoformat(prefs["daily_check_time"]), step=60)
        channel = st.selectbox("提醒渠道", ["auto", "terminal"], index=0 if prefs["channel"] == "auto" else 1,
                               format_func=lambda value: "优先 Windows 桌面通知，失败时终端与日志" if value == "auto" else "终端与日志")
        if st.form_submit_button("保存个人设置", type="primary"):
            try:
                try:
                    lead_days = [int(day.strip()) for day in days.replace("，", ",").split(",")]
                except ValueError:
                    raise ValidationError("提醒天数请使用逗号分隔的整数") from None
                service.update_preferences(enabled=enabled, lead_days=lead_days, check_time=clock.strftime("%H:%M"), channel=channel)
                flash("个人提醒偏好已保存。")
                st.rerun()
            except KNOWN_ERRORS as exc:
                show_error(exc)
    st.info("保存每日时间只修改个人偏好。Windows 定时任务仍需用辅助脚本重新预览并确认；页面不会修改系统计划。关闭网页后，已注册的 Windows 计划仍可独立检查。")
    with st.expander("查看定时任务配置方法"):
        st.code("powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\configure_scheduler.ps1", language="powershell")
        st.caption("以上命令只预览；核对后按 README 的说明手动确认注册/更新。")
    st.subheader("我的课程")
    st.caption("只需填写你实际学习的课程全名。AI 会结合通知内容判断简称对应哪门课；无法确定时标记待确认，无需维护简称对照表。")
    courses = service.courses()
    if courses:
        st.dataframe([{"课程名称": item["name"]} for item in courses], hide_index=True, width="stretch")
    else:
        st.caption("尚未手动登记课程；已有任务中的课程同样会用于解析上下文。")
    with st.form("add_course", enter_to_submit=False):
        name = st.text_input("添加标准课程名称")
        if st.form_submit_button("保存课程"):
            try:
                service.add_course(name)
                flash("课程已保存，后续解析会使用该课程上下文。")
                st.rerun()
            except KNOWN_ERRORS as exc:
                show_error(exc)
    st.subheader("本地数据与备份")
    st.text("任务数据目录：" + str(service.store.root))
    if st.button("备份当前数据", key="backup"):
        try:
            paths = service.backup()
            st.success(f"已创建 {len(paths)} 份数据文件备份。")
            st.text("\n".join(str(path) for path in paths))
        except KNOWN_ERRORS as exc:
            show_error(exc)


def main():
    st.set_page_config(page_title="校园课程管家", page_icon="📚", layout="wide", initial_sidebar_state="collapsed")
    st.html(STYLE)  # 固定CSS进入事件容器，不额外占据顶部布局空间；不拼入用户文本。
    service = TaskService(os.environ.get("COURSE_ASSISTANT_DATA_DIR") or DEFAULT_DATA_DIR)
    try:
        service.initialize()
        now = now_shanghai()
        snapshot = dashboard.overview(service, now)
        title_col, refresh_col = st.columns([5, 1], vertical_alignment="center")
        title_col.title("校园课程管家")
        if refresh_col.button("刷新数据", width="stretch"):
            st.rerun()
        st.caption(now.strftime("%Y年%m月%d日 %H:%M") + " · 上海时间 · 本地任务台账")
        with st.container(key="main_navigation"):
            page = st.radio("工作区", ["任务总览", "通知导入", "个人设置"], key="page", horizontal=True, label_visibility="collapsed")
        if "flash" in st.session_state:
            st.success(st.session_state.pop("flash"))
        if "operation_error" in st.session_state:
            st.error(st.session_state.pop("operation_error"))
        if page == "任务总览":
            render_overview(service, snapshot, now)
        elif page == "通知导入":
            render_import(service)
        else:
            render_settings(service, snapshot["config"])
    except Exception as exc:
        show_error(exc)
        st.info("如数据校验失败，请先检查 data/backups 与日志。程序不会覆盖损坏文件或创建展示任务。")
