"""通知关系计划与提交：乐观版本检查、一次原子写入任务及去重凭据。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from .cache import digest, notice_id
from .errors import ValidationError
from .interfaces import ParseContext, TaskDraft
from .matching import SemanticMatcher, build_patch, identity
from .models import Deadline, Notice, Task, notice_time_bounds, timestamp


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    draft: TaskDraft
    target_ids: tuple[str, ...]
    reason: str
    evidence: str = ""
    patch: dict = field(default_factory=dict)
    diff: dict = field(default_factory=dict)
    issues: tuple[str, ...] = ()

    def preview(self) -> dict:
        labels = {"new": "新任务", "duplicate": "重复通知", "update": "已有任务的修改通知", "uncertain": "无法确定，需要人工判断"}
        change_types = []
        if "deadline" in self.diff:
            before, after = self.diff["deadline"]["before"], self.diff["deadline"]["after"]
            if before["date"] and after["date"] and before["date"] != after["date"]:
                change_types.append("延期" if after["date"] > before["date"] else "提前")
            else: change_types.append("截止日期/时间变更")
        if "submission" in self.diff:
            for field, label in (("method", "提交方式变更"), ("format_requirements", "提交格式变更")):
                if self.diff["submission"]["before"][field] != self.diff["submission"]["after"][field]: change_types.append(label)
        if "deleted_at" in self.diff: change_types.append("取消/软删除")
        return {"classification": labels[self.kind], "kind": self.kind, "candidate_ids": self.target_ids, "change_types": change_types,
                "reason": self.reason, "evidence": self.evidence, "differences": self.diff,
                "issues": self.issues, "draft": asdict(self.draft)}


@dataclass(frozen=True)
class NoticePlan:
    batch_id: str
    base_revision: str
    actions: tuple[PlannedAction, ...] = ()
    warnings: tuple[str, ...] = ()
    already_processed: bool = False


class NoticeWorkflow:
    def __init__(self, service, parser, matcher: SemanticMatcher):
        self.service, self.parser, self.matcher = service, parser, matcher

    def prepare(self, notice: Notice, context: ParseContext, *, target_id: str | None = None,
                as_new: bool = False, due_override: Deadline | None = None) -> NoticePlan:
        document = self.service.document()
        if target_id and target_id not in {value["id"] for value in document["tasks"]}:
            raise ValidationError("人工选择的任务 ID 不存在；未调用模型")
        if due_override is not None and target_id is None and not as_new:
            raise ValidationError("--due 必须同时指定 --target 或 --as-new；未调用模型")
        if target_id and as_new:
            raise ValidationError("不能同时指定旧任务目标和新建")
        batch = notice_id(notice)
        if any(receipt["id"] == batch for receipt in document.get("processed_batches", [])):
            return NoticePlan(batch, digest(document), already_processed=True)
        report = self.parser.analyze(notice, context)
        # 网络调用期间可能有人改台账；用最新单文件快照构建计划。
        document = self.service.document()
        existing = [Task.from_dict(value) for value in document["tasks"]]
        if (target_id or as_new or due_override) and len(report.drafts) != 1:
            raise ValidationError("人工指定目标/新建/截止时间仅支持单任务通知，请拆分后处理")
        if due_override is not None and target_id is None and not as_new:
            raise ValidationError("--due 必须同时指定 --target 或 --as-new")
        seen, actions = {}, []
        for index, draft in enumerate(report.drafts):
            signature = identity(draft)
            if signature in seen:
                actions.append(PlannedAction("duplicate", draft, (f"batch:{seen[signature]}",), "本批次已有相同结构化任务，跳过重复项"))
                continue
            seen[signature] = index
            relation = self.matcher.analyze(draft, existing)
            decision = relation.decision
            if decision.kind == "duplicate":
                actions.append(PlannedAction("duplicate", draft, decision.related_task_ids, decision.reason))
                continue
            if as_new:
                updated_draft = replace(draft, deadline=due_override) if due_override else draft
                actions.append(PlannedAction("new", updated_draft, (), "用户显式选择新任务；" + decision.reason))
                continue
            ids = (target_id,) if target_id else decision.related_task_ids
            if target_id or decision.kind == "possible_update":
                if len(ids) != 1 or ids[0] not in {t.id for t in existing}:
                    raise ValidationError("人工选择的任务 ID 不存在或匹配目标不唯一")
                old = next(t for t in existing if t.id == ids[0])
                changes = relation.changes
                if target_id and not changes:
                    # 用户明确指定目标时，仅提出有新值的截止/提交字段；不把泛化名称覆盖旧任务。
                    raw = notice.raw_notice
                    fields = []
                    if draft.deadline.date or due_override: fields.append(("deadline", raw))
                    if draft.submission.method: fields.append(("submission.method", raw))
                    if draft.submission.format_requirements: fields.append(("submission.format_requirements", raw))
                    changes = tuple(fields)
                patch, diff, issues = build_patch(draft, old, changes, due_override=due_override)
                requested = {name for name, _ in changes}
                blocked = (old.deleted_at is not None or not diff or
                           ("deadline" in requested and "deadline" not in patch) or
                           any(name.startswith("submission.") and not getattr(draft.submission, name.split(".")[1]) for name in requested) or
                           ("deleted_at" in patch and set(patch) - {"deleted_at", "review"}))
                if old.deleted_at:
                    issues.append("旧任务已软删除，请先撤销删除；不会自动恢复")
                if "deadline" in patch and old.deadline.date:
                    import re
                    after, before = patch["deadline"].date, old.deadline.date
                    evidence = " ".join(quote for name, quote in changes if name == "deadline")
                    conflict = (re.search("延期|延后|推迟", evidence) and after < before) or ("提前" in evidence and after > before)
                    if conflict:
                        issues.append("延期/提前措辞与日期先后冲突，需通过 --target 和 --due 明确核实日期")
                        blocked = blocked or due_override is None
                # 新通知比已知通知早时，不允许直接采用旧消息覆盖新版本。
                latest_sources = [old.source, *(Notice.from_dict(entry.changes["notice"]) for entry in old.history if "notice" in entry.changes)]
                known_times = [notice_time_bounds(source.sent_at) for source in latest_sources if source.sent_at]
                if notice.sent_at and known_times:
                    start, end = notice_time_bounds(notice.sent_at)
                    if any(end < old_start for old_start, _ in known_times):
                        issues.append("通知发送时间早于已保留通知，可能为过时信息；请人工核实")
                        blocked = blocked or not bool(target_id)
                    elif any((start != end or old_start != old_end) and start <= old_end and old_start <= end
                             for old_start, old_end in known_times):
                        issues.append("同日通知的发送时刻不完整，无法确认先后；请人工选择目标并核实")
                        blocked = blocked or not bool(target_id)
                kind = "uncertain" if blocked else "update"
                actions.append(PlannedAction(kind, draft, ids, decision.reason, relation.evidence, patch, diff, tuple(issues)))
            elif decision.kind == "new":
                actions.append(PlannedAction("new", draft, (), decision.reason, relation.evidence))
            else:
                actions.append(PlannedAction("uncertain", draft, ids, decision.reason, relation.evidence))
        # 同一批两个不同修改指向一个旧任务，不按模型输出顺序隐式覆盖。
        targets = [a.target_ids[0] for a in actions if a.kind == "update"]
        actions = [replace(a, kind="uncertain", issues=(*a.issues, "本批次多个修改指向同一任务，请拆分核实"))
                   if a.kind == "update" and targets.count(a.target_ids[0]) > 1 else a for a in actions]
        return NoticePlan(batch, digest(document), tuple(actions), report.warnings)

    def apply(self, plan: NoticePlan, *, confirmed: bool = False) -> dict:
        if confirmed is not True:
            raise ValidationError("必须展示关系、差异并获得人工确认")
        if plan.already_processed:
            return {"already_processed": True, "created": [], "updated": [], "duplicates": 0}
        if any(action.kind == "uncertain" for action in plan.actions):
            raise ValidationError("存在无法可靠匹配的通知，请先选择目标或明确新建；台账未修改")
        with self.service.store.lock():
            config = self.service.config()
            if not config["ai_enabled"]:
                raise ValidationError("AI 通知处理已关闭")
            document = self.service.document()
            receipts = document.get("processed_batches", [])
            if any(receipt["id"] == plan.batch_id for receipt in receipts):
                return {"already_processed": True, "created": [], "updated": [], "duplicates": 0}
            if digest(document) != plan.base_revision:
                raise ValidationError("预览后台账已变化，请重新预览确认；没有覆盖并发更新")
            tasks = [Task.from_dict(value) for value in document["tasks"]]
            created, updated, duplicates = [], [], 0
            for action in plan.actions:
                if action.kind == "new":
                    draft = action.draft
                    task = Task.create(course=draft.course, title=draft.title, description=draft.description,
                                       deadline=draft.deadline, submission=draft.submission, source=draft.source, review=draft.review)
                    tasks.append(task); created.append(task.id)
                elif action.kind == "duplicate":
                    duplicates += 1
                elif action.kind == "update":
                    index = next((i for i, task in enumerate(tasks) if task.id == action.target_ids[0]), None)
                    if index is None:
                        raise ValidationError("匹配目标不存在")
                    patch = dict(action.patch)
                    if "deleted_at" in patch: patch["deleted_at"] = timestamp()
                    action_name = "task_deleted" if "deleted_at" in patch else "task_updated"
                    tasks[index] = tasks[index].mutate(patch, action_name, notice=action.draft.source, reason=action.reason)
                    updated.append(tasks[index].id)
                else:
                    raise ValidationError("未知处理动作")
            receipt = {"id": plan.batch_id, "at": timestamp(), "task_ids": list(dict.fromkeys([*created, *updated,
                       *(target for action in plan.actions if action.kind == "duplicate" for target in action.target_ids if not target.startswith("batch:"))])),
                       "outcomes": [a.kind for a in plan.actions]}
            self.service._save_tasks(tasks, config, receipts=[*receipts, receipt])
            self.service.store.audit("notice_batch_applied", details={"batch_id": plan.batch_id, "created": created,
                                                                        "updated": updated, "duplicates": duplicates})
            return {"already_processed": False, "created": created, "updated": updated, "duplicates": duplicates, "imported": len(created)}
