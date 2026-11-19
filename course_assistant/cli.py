"""Windows/跨平台命令行入口；录入字段全部由用户明确提供。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import sys

from .errors import ModelError, StorageError, TaskNotFoundError, ValidationError
from .models import Deadline, Notice, Submission, aware_datetime
from .service import DEFAULT_DATA_DIR, TaskService


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="课程通知助理 · 第5轮验收版：AI 解析、课程管理、可恢复更新与本地提醒")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="数据目录；放在子命令前面")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化或验证数据文件，不覆盖已有内容")
    add = commands.add_parser("add", help="人工填写并新增任务")
    add.add_argument("--course", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--due", help="YYYY-MM-DD 或 YYYY-MM-DDTHH:MM[:SS]；省略则时间未知")
    add.add_argument("--needs-confirmation", action="store_true", help="即使精确到分钟/秒也保留待确认标记")
    add.add_argument("--submission", default="", help="提交方式")
    add.add_argument("--format", dest="format_requirements", default="", help="格式要求")
    notice = add.add_mutually_exclusive_group()
    notice.add_argument("--notice", default="", help="原始通知文本，不进行语义解析")
    notice.add_argument("--notice-file", type=Path, help="从 UTF-8 文本文件逐字读取原始通知")
    add.add_argument("--sent-at", help="原通知发送日期YYYY-MM-DD，或包含时区的ISO日期时间；不补造钟点")
    listing = commands.add_parser("list", help="查询任务，默认按截止时间升序排列")
    listing.add_argument("--status", choices=("all", "pending", "completed"), default="all")
    listing.add_argument("--course", help="课程名称精确匹配")
    listing.add_argument("--query", help="在课程、作业名称、内容中做普通子串查找")
    listing.add_argument("--sort", choices=("deadline", "course", "created"), default="deadline")
    listing.add_argument("--descending", action="store_true")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--include-deleted", action="store_true", help="同时显示软删除任务")
    for name, help_text in (("show", "查看任务全部字段与历史"), ("complete", "标记完成，重复执行不重复记录")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("id", help="完整任务 UUID")
    change = commands.add_parser("set-deadline", help="人工修正截止时间并记录前后值")
    change.add_argument("id")
    due = change.add_mutually_exclusive_group(required=True)
    due.add_argument("--due")
    due.add_argument("--clear", action="store_true", help="设为未知、待确认")
    change.add_argument("--needs-confirmation", action="store_true")
    courses = commands.add_parser("courses", help="维护可选的课程目录")
    course_commands = courses.add_subparsers(dest="course_command", required=True)
    course_commands.add_parser("list")
    course_add = course_commands.add_parser("add")
    course_add.add_argument("--name", required=True)
    commands.add_parser("backup", help="为三个 JSON 文件创建手动快照")
    deletion = commands.add_parser("delete", help="预览并确认软删除，可撤销")
    deletion.add_argument("id")
    undo = commands.add_parser("undo", help="预览并撤销最近一次修改；不指定 ID 则选全局最近修改")
    undo.add_argument("id", nargs="?")
    parsing = commands.add_parser("parse", help="调用真实模型解析通知，预览后人工确认导入")
    source = parsing.add_mutually_exclusive_group()
    source.add_argument("--text", help="直接传入通知文本")
    source.add_argument("--file", type=Path, help="UTF-8 通知文件；无输入选项时读取当前目录 inbox.txt")
    source.add_argument("--paste", action="store_true", help="交互粘贴多行，单独一行 ::end 结束")
    parsing.add_argument("--sent-at", help="消息实际发送日期YYYY-MM-DD，或包含时区的ISO日期时间；省略则不计算相对日期")
    parsing.add_argument("--settings", type=Path, help="真实校历配置文件（兼容旧版别名配置）")
    parsing.add_argument("--preview-only", action="store_true", help="只解析预览，永不导入、不询问确认")
    parsing.add_argument("--refresh", action="store_true", help="显式刷新模型缓存，可能重新付费；不绕过已处理保护")
    choice = parsing.add_mutually_exclusive_group()
    choice.add_argument("--target", help="人工选择唯一旧任务完整 ID，仍须预览确认")
    choice.add_argument("--as-new", action="store_true", help="人工将不明确的单任务通知作为新任务；不绕过确定重复")
    parsing.add_argument("--due", help="配合 --target/--as-new 人工核实完整截止时间")
    agenda = commands.add_parser("agenda", help="未来7天未完成任务与需补充时间的信息，按截止时间排列")
    agenda.add_argument("--days", type=int, default=7)
    agenda.add_argument("--course")
    agenda.add_argument("--now", help="可选的带时区查询时间，只读")
    prefs = commands.add_parser("preferences", help="查看或修改 config.json 中的个人提醒偏好")
    prefs_commands = prefs.add_subparsers(dest="prefs_command", required=True)
    prefs_commands.add_parser("show")
    prefs_set = prefs_commands.add_parser("set")
    switch = prefs_set.add_mutually_exclusive_group()
    switch.add_argument("--enable", dest="enabled", action="store_true")
    switch.add_argument("--disable", dest="enabled", action="store_false")
    prefs_set.set_defaults(enabled=None)
    prefs_set.add_argument("--lead-days", nargs="+", type=int)
    prefs_set.add_argument("--check-time")
    prefs_set.add_argument("--channel", choices=("auto", "terminal"))
    remind = commands.add_parser("remind", help="手动提醒检查、状态、真实桌面通知测试；不注册系统任务")
    remind_commands = remind.add_subparsers(dest="remind_command", required=True)
    check = remind_commands.add_parser("check")
    check.add_argument("--dry-run", action="store_true", help="只写标记为预览的运行日志，不通知、不消费节点")
    check.add_argument("--now", help="模拟时间，只能与 --dry-run 同用")
    check.add_argument("--channel", choices=("auto", "terminal"))
    check.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")
    remind_commands.add_parser("status")
    probe = remind_commands.add_parser("test-notification")
    probe.add_argument("--channel", choices=("auto", "terminal"), default="auto")
    return parser


def _parse_notice(args: argparse.Namespace, service: TaskService) -> None:
    from .interfaces import ParseContext
    from .llm import ApiSettings, StructuredClient
    from .models import notice_time
    from .parsing import AINotificationParser, DEFAULT_SETTINGS, ParserSettings
    from .cache import CachedClient
    from .schema import PARSE_SCHEMA
    from .matching import RELATION_SCHEMA, SemanticMatcher
    from .workflow import NoticeWorkflow
    if not service.config()["ai_enabled"]:
        raise ValidationError("config.json 中 ai_enabled 为 false；启用后才可显式调用 parse")
    if args.paste:
        print("粘贴群通知，单独输入一行 ::end 结束；发送时间使用 --sent-at 明确提供。")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "::end":
                break
            lines.append(line)
        raw = "\n".join(lines)
    elif args.text is not None:
        raw = args.text
    else:
        with (args.file or Path("inbox.txt")).open(encoding="utf-8-sig", newline="") as handle:
            raw = handle.read()
    notice = Notice(raw, args.sent_at)
    settings = ParserSettings.load(args.settings or DEFAULT_SETTINGS)
    context = ParseContext(notice_time(notice.sent_at), "+08:00",
                           tuple(sorted({course["name"] for course in service.courses()} |
                                        {task.course for task in service.list() if "course" not in task.review.uncertain_fields})))
    parser = AINotificationParser(CachedClient(service.store.root, lambda: StructuredClient(ApiSettings.from_env()), PARSE_SCHEMA,
                                             stage="parse", refresh=args.refresh), settings)
    relation_client = CachedClient(service.store.root, lambda: StructuredClient(ApiSettings.from_env(), schema=RELATION_SCHEMA, schema_name="notice_relation"),
                                   RELATION_SCHEMA, stage="relations", refresh=args.refresh)
    workflow = NoticeWorkflow(service, parser, SemanticMatcher(relation_client, settings.aliases))
    due = Deadline.from_input(args.due) if args.due else None
    plan = workflow.prepare(notice, context, target_id=args.target, as_new=args.as_new, due_override=due)
    if plan.already_processed:
        print("该批通知已处理，未调用模型、未重复导入。撤销后也不会自动重放旧通知。")
        return
    print("解析预览（尚未写入任务台账）：")
    ids = {target for action in plan.actions for target in action.target_ids}
    old = [{"id": task.id, **task.state()} for task in service.list(include_deleted=True) if task.id in ids]
    _json({"warnings": plan.warnings, "actions": [action.preview() for action in plan.actions], "existing_candidates": old})
    if not plan.actions:
        print("未识别到课程任务，没有导入。")
        return
    if args.preview_only:
        print("仅预览，没有导入。")
        return
    if any(action.kind == "uncertain" for action in plan.actions):
        print("匹配尚不明确，没有导入或覆盖。单任务可重新运行并使用 --target 选择旧 ID，或 --as-new 明确新建；日期不完整时可配合 --due。")
        return
    if all(action.kind == "duplicate" for action in plan.actions):
        _json(workflow.apply(plan, confirmed=True))
        print("全部为重复通知，没有新增或覆盖任务。")
        return
    if not sys.stdin.isatty():
        print("非交互输入仅允许预览；请在终端重新运行并人工确认。没有导入。")
        return
    has_issues = any(action.draft.review.needs_confirmation or action.issues for action in plan.actions if action.kind != "duplicate")
    token = secrets.token_hex(3)
    verb = "更新" if any(action.kind == "update" for action in plan.actions) else "导入"
    phrase = f"确认{verb}" + ("并保留待确认 " if has_issues else " ") + token
    print("请核对原文、课程、日期和提交要求；发现错误请取消后补充信息重新解析。")
    if has_issues:
        print("仍有待确认字段；导入后这些标记将保留，不代表已经核实。")
    try:
        answer = input(f"输入【{phrase}】提交上述处理计划；其他输入取消：")
    except EOFError:
        answer = ""
    if answer.strip() != phrase:
        print("已取消，没有导入。")
        return
    _json(workflow.apply(plan, confirmed=True))


def _confirm_action(label: str) -> bool:
    if not sys.stdin.isatty():
        print("非交互输入仅展示预览，没有执行修改。")
        return False
    phrase = f"确认{label} {secrets.token_hex(3)}"
    try:
        answer = input(f"输入【{phrase}】执行，其他输入取消：")
    except EOFError:
        answer = ""
    if answer.strip() != phrase:
        print("已取消，没有修改。")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    # 统一 PowerShell 重定向和中文输出编码；不替换测试传入的 StringIO。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        service = TaskService(args.data_dir)
        if args.command == "remind":
            from .reminders import ReminderRunner
            runner = ReminderRunner(service)
            if args.remind_command == "check":
                if args.now and not args.dry_run: raise ValidationError("--now 模拟时间只能配合 --dry-run，不得用于真实通知或状态")
                result = runner.check(now=aware_datetime(args.now, "now") if args.now else None,
                                      dry_run=args.dry_run, channel=args.channel, trigger=args.trigger)
                _json(result)
            elif args.remind_command == "status":
                service.initialize()
                _json(runner.status())
            else:
                _json(runner.test_notification(channel=args.channel))
            return 0
        created = service.initialize()
        if args.command == "init":
            _json({"data_dir": str(service.store.root), "created_files": created, "valid": True})
        elif args.command == "add":
            if args.notice_file:
                with args.notice_file.open(encoding="utf-8-sig", newline="") as handle:
                    raw_notice = handle.read()
            else:
                raw_notice = args.notice
            deadline = Deadline.from_input(args.due, service.config()["utc_offset"], needs_confirmation=args.needs_confirmation)
            task = service.add(course=args.course, title=args.title, description=args.description, deadline=deadline,
                               submission=Submission(args.submission, args.format_requirements), source=Notice(raw_notice, args.sent_at))
            _json(task.to_dict())
        elif args.command == "list":
            tasks = service.list(status=args.status, course=args.course, query=args.query, sort=args.sort, descending=args.descending, include_deleted=args.include_deleted)
            if args.json:
                _json([task.to_dict() for task in tasks])
            elif not tasks:
                print("没有符合条件的任务。")
            else:
                print("ID\t状态\t课程\t作业\t截止时间\t精度\t待确认")
                for task in tasks:
                    deadline = task.deadline
                    due = deadline.date or "未知"
                    if deadline.time:
                        due += f" {deadline.time} {deadline.utc_offset}"
                    fields = (task.id, "已删除" if task.deleted_at else "已完成" if task.status == "completed" else "待办", task.course, task.title,
                              due, deadline.precision, "是" if deadline.needs_confirmation or task.review.needs_confirmation else "否")
                    print("\t".join(field.replace("\n", " ").replace("\r", " ").replace("\t", " ") for field in fields))
        elif args.command == "show":
            _json(service.get(args.id).to_dict())
        elif args.command in ("complete", "set-deadline"):
            if args.command == "complete":
                result = service.complete(args.id)
            else:
                deadline = Deadline.from_input(args.due, service.config()["utc_offset"], needs_confirmation=args.needs_confirmation)
                current = service.get(args.id)
                proposed = current.set_deadline(deadline)
                if proposed == current:
                    _json({"changed": False, "task": current.to_dict()})
                    return 0
                _json({"preview": "截止时间修改，尚未保存", "id": current.id,
                       "before": asdict(current.deadline), "after": asdict(deadline)})
                if not _confirm_action("修改截止时间"):
                    return 0
                result = service.set_deadline(args.id, deadline, expected_version=current.updated_at)
            _json({"changed": result.changed, "task": result.task.to_dict()})
        elif args.command in ("delete", "undo"):
            task_id = args.id
            if args.command == "undo" and task_id is None:
                tasks = [task for task in service.list(include_deleted=True) if task.history[-1].action != "created"]
                if not tasks:
                    raise ValidationError("没有可撤销的任务修改")
                task_id = max(tasks, key=lambda task: aware_datetime(task.updated_at, "updated_at")).id
            current = service.get(task_id)
            proposed = current.undo() if args.command == "undo" else None
            _json({"preview": "撤销最近修改" if proposed else "软删除任务", "id": current.id,
                   "before": current.state(), "after": proposed.state() if proposed else {"deleted_at": "确认后设置；任务可恢复"}})
            if _confirm_action("撤销" if proposed else "删除"):
                task = service.recoverable_action(current.id, action=args.command, confirmed=True, expected_version=current.updated_at)
                _json(task.to_dict())
        elif args.command == "courses":
            _json(service.courses() if args.course_command == "list" else service.add_course(args.name))
        elif args.command == "backup":
            _json({"backups": [str(path) for path in service.backup()]})
        elif args.command == "parse":
            _parse_notice(args, service)
        elif args.command == "agenda":
            from datetime import datetime
            from .models import parse_offset
            from .reminders import upcoming
            zone = parse_offset(service.config()["utc_offset"])
            now = aware_datetime(args.now, "now").astimezone(zone) if args.now else datetime.now(zone)
            _json(upcoming(service.list(), now, days=args.days, course=args.course))
        elif args.command == "preferences":
            if args.prefs_command == "show": _json(service.config())
            else: _json(service.update_preferences(enabled=args.enabled, lead_days=args.lead_days, check_time=args.check_time, channel=args.channel))
        return 0
    except (StorageError, TaskNotFoundError, ValidationError, ModelError, OSError, UnicodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
