"""第3轮全为模拟通知/语义响应，不代表真实 API 或个人使用证据。"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from course_assistant.cache import CachedClient
from course_assistant.errors import ModelError, StorageError, ValidationError
from course_assistant.interfaces import ParseContext, TaskDraft
from course_assistant.matching import RELATION_SCHEMA, SemanticMatcher, exact_duplicate
from course_assistant.models import Deadline, Notice, Submission, Task, TaskReview
from course_assistant.schema import PARSE_SCHEMA
from course_assistant.service import TaskService
from course_assistant.workflow import NoticeWorkflow
from tests.parser_fixtures import FakeClient, response

ORIGINAL = "【模拟数据】微观经济学作业11月25日截止。"
FOLLOWUP = "【模拟数据】上次的微观作业延期到11月27日，其他要求不变。"
SENT = "2026-11-18T18:00:00+08:00"
CONTEXT = ParseContext(None, "+08:00", ())


class FakeParser:
    def __init__(self, drafts): self.drafts, self.calls = tuple(drafts), 0
    def analyze(self, notice, context):
        self.calls += 1
        return SimpleNamespace(drafts=self.drafts, warnings=())


def draft(raw=FOLLOWUP, *, due=None, method="", formats="", title="上次的微观作业", course="中级微观经济学", sent_at=SENT):
    return TaskDraft(course, title, raw, Deadline.from_input(due), Submission(method, formats), Notice(raw, sent_at),
                     TaskReview(True, ("deadline",), ("【模拟】时间待确认",)))


def relation(targets, *, kind="possible_update", changes=(("deadline", "延期到11月27日"),), evidence="上次的微观作业", reason="同一微观课程，上次作业是原任务指代，只有日期改变"):
    return {"kind": kind, "target_ids": list(targets), "reason": reason, "evidence": evidence,
            "changes": [{"field": name, "evidence": quote} for name, quote in changes]}


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = TaskService(self.root); self.service.initialize()
        self.old = self.service.add(course="微观经济学", title="微观经济学作业", description="课后题1至5",
                                    deadline=Deadline.from_input("2026-11-25"), submission=Submission("教学平台", "PDF"),
                                    source=Notice(ORIGINAL, "2026-11-17T18:00:00+08:00"))

    def workflow(self, item=None, output=None):
        item = item or draft()
        parser = FakeParser([item])
        client = FakeClient(output if output is not None else relation([self.old.id]))
        return NoticeWorkflow(self.service, parser, SemanticMatcher(client)), parser, client

    def test_focus_postponement_has_diff_and_no_write_before_confirm(self):
        workflow, _, client = self.workflow()
        plan = workflow.prepare(draft().source, CONTEXT)
        action = plan.actions[0]
        self.assertEqual(action.kind, "update")
        self.assertEqual(action.target_ids, (self.old.id,))
        self.assertEqual(action.diff["deadline"]["before"]["date"], "2026-11-25")
        self.assertEqual(action.diff["deadline"]["after"]["date"], "2026-11-27")
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")
        self.assertTrue(client.calls)
        self.assertIn("candidates", client.calls[0][1])
        with self.assertRaises(ValidationError): workflow.apply(plan)
        result = workflow.apply(plan, confirmed=True)
        self.assertEqual(result["updated"], [self.old.id])
        updated = self.service.get(self.old.id)
        self.assertEqual(updated.deadline.date, "2026-11-27")
        self.assertEqual(updated.submission, self.old.submission)
        self.assertEqual(updated.title, self.old.title)
        self.assertEqual(updated.source.raw_notice, ORIGINAL)
        self.assertEqual(updated.history[-1].changes["notice"]["raw_notice"], FOLLOWUP)
        self.assertTrue(updated.deadline.needs_confirmation)

    def test_legacy_partial_deadline_update_accepts_date_only_sending_time(self):
        item = draft(sent_at="2026-11-18")
        workflow, _, _ = self.workflow(item)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "update")
        self.assertEqual(plan.actions[0].patch["deadline"].date, "2026-11-27")

    def test_invalid_manual_target_rejected_before_paid_parse(self):
        workflow, parser, client = self.workflow()
        with self.assertRaises(ValidationError): workflow.prepare(draft().source, CONTEXT, target_id="not-an-existing-task")
        self.assertEqual(parser.calls, 0)
        self.assertEqual(client.calls, [])

    def test_advance_and_submission_changes_are_sparse(self):
        raw = "【模拟数据】微观作业提前到2026年11月23日，改交Word，发教学邮箱。"
        item = draft(raw, due="2026-11-23", formats="Word", method="教学邮箱")
        output = relation([self.old.id], changes=(("deadline", "提前到2026年11月23日"), ("submission.format_requirements", "改交Word"), ("submission.method", "发教学邮箱")), evidence="微观作业")
        workflow, _, _ = self.workflow(item, output)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(set(plan.actions[0].diff), {"deadline", "submission", "review"})
        workflow.apply(plan, confirmed=True)
        updated = self.service.get(self.old.id)
        self.assertEqual(updated.submission, Submission("教学邮箱", "Word"))
        self.assertEqual(updated.deadline.date, "2026-11-23")

    def test_format_only_does_not_erase_deadline_or_channel(self):
        raw = "【模拟数据】微观作业格式改为Word，其他不变。"
        item = draft(raw, formats="Word")
        output = relation([self.old.id], changes=(("submission.format_requirements", "格式改为Word"),), evidence="微观作业")
        workflow, _, _ = self.workflow(item, output)
        workflow.apply(workflow.prepare(item.source, CONTEXT), confirmed=True)
        updated = self.service.get(self.old.id)
        self.assertEqual(updated.deadline, self.old.deadline)
        self.assertEqual(updated.submission, Submission("教学平台", "Word"))

    def test_changed_requirement_uncertainty_is_not_lost(self):
        raw = "【模拟数据】微观作业格式可能改为Word，请核实。"
        item = replace(draft(raw, formats="Word"), review=TaskReview(True, ("submission.format_requirements",), ("需核实格式",)))
        output = relation([self.old.id], changes=(("submission.format_requirements", "格式可能改为Word"),), evidence="微观作业")
        workflow, _, _ = self.workflow(item, output)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertIn("submission.format_requirements", plan.actions[0].patch["review"].uncertain_fields)

    def test_manual_verified_due_clears_model_uncertainty(self):
        with self.service.store.lock():
            old = self.old.mutate({"review": TaskReview(True, ("deadline",), ("未核实时间",))}, "task_updated")
            self.service._save_tasks([old], self.service.config())
        workflow, _, _ = self.workflow()
        plan = workflow.prepare(draft().source, CONTEXT, target_id=self.old.id, due_override=Deadline.from_input("2026-11-27T23:30"))
        self.assertNotIn("deadline", plan.actions[0].patch["review"].uncertain_fields)

    def test_multiple_candidates_never_select_arbitrary_target(self):
        other = self.service.add(course="微观经济学", title="第2次作业", description="另一独立作业")
        workflow, _, _ = self.workflow(output=relation([self.old.id, other.id], kind="uncertain"))
        plan = workflow.prepare(draft().source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "uncertain")
        self.assertEqual(set(plan.actions[0].target_ids), {self.old.id, other.id})
        with self.assertRaises(ValidationError): workflow.apply(plan, confirmed=True)
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-25")
        chosen = workflow.prepare(draft().source, CONTEXT, target_id=self.old.id)
        self.assertEqual(chosen.actions[0].kind, "update")

    def test_semantic_new_is_not_deduplicated_just_because_same_course(self):
        item = draft("【模拟数据】微观经济学第2次作业，主题为消费者选择。", title="第2次作业", course="微观经济学", due="2026-12-01")
        output = relation([], kind="new", changes=(), evidence="第2次作业", reason="主题和作业编号不同，是独立任务")
        workflow, _, _ = self.workflow(item, output)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "new")
        workflow.apply(plan, confirmed=True)
        self.assertEqual(len(self.service.list()), 2)

    def test_semantic_duplicate_paraphrase_does_not_create_task(self):
        raw = "【模拟数据】提醒一下，微观的课后练习一到五仍然25日交。"
        item = draft(raw)
        output = relation([self.old.id], kind="duplicate", changes=(), evidence="课后练习一到五", reason="课后练习一到五和课后题1至5是同一任务，没有新增要求")
        workflow, _, _ = self.workflow(item, output)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "duplicate")
        workflow.apply(plan, confirmed=True)
        self.assertEqual(len(self.service.list()), 1)

    def test_conflicting_deadline_cannot_be_ignored_as_duplicate(self):
        item = draft(due="2026-11-27")
        workflow, _, _ = self.workflow(item, relation([self.old.id], kind="duplicate", changes=()))
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "uncertain")
        with self.assertRaises(ValidationError): workflow.apply(plan, confirmed=True)

    def test_user_rejection_leaves_history_and_receipts_unchanged(self):
        workflow, _, _ = self.workflow()
        before = self.service.document()
        plan = workflow.prepare(draft().source, CONTEXT)
        with self.assertRaises(ValidationError): workflow.apply(plan, confirmed=False)
        self.assertEqual(self.service.document(), before)

    def test_repeated_batch_checked_before_parser_and_model(self):
        workflow, parser, client = self.workflow()
        workflow.apply(workflow.prepare(draft().source, CONTEXT), confirmed=True)
        calls = (parser.calls, len(client.calls))
        again = workflow.prepare(draft().source, CONTEXT)
        self.assertTrue(again.already_processed)
        self.assertEqual((parser.calls, len(client.calls)), calls)
        self.assertTrue(workflow.apply(again, confirmed=True)["already_processed"])

    def test_duplicate_commit_is_idempotent_even_with_stale_plan(self):
        workflow, _, _ = self.workflow()
        plan = workflow.prepare(draft().source, CONTEXT)
        workflow.apply(plan, confirmed=True)
        self.assertTrue(workflow.apply(plan, confirmed=True)["already_processed"])
        self.assertEqual(len(self.service.get(self.old.id).history), 2)

    def test_stale_preview_rejected_after_concurrent_change(self):
        workflow, _, _ = self.workflow()
        plan = workflow.prepare(draft().source, CONTEXT)
        self.service.set_deadline(self.old.id, Deadline.from_input("2026-11-26"))
        with self.assertRaisesRegex(ValidationError, "台账已变化"): workflow.apply(plan, confirmed=True)
        self.assertEqual(self.service.get(self.old.id).deadline.date, "2026-11-26")

    def test_task_and_receipt_write_failure_is_atomic(self):
        workflow, _, _ = self.workflow()
        plan = workflow.prepare(draft().source, CONTEXT)
        before = (self.root / "deadlines.json").read_bytes()
        with patch.object(self.service.store, "write", side_effect=StorageError("【模拟】磁盘失败")):
            with self.assertRaises(StorageError): workflow.apply(plan, confirmed=True)
        self.assertEqual((self.root / "deadlines.json").read_bytes(), before)

    def test_undo_restores_snapshot_and_preserves_all_notices(self):
        workflow, _, _ = self.workflow()
        workflow.apply(workflow.prepare(draft().source, CONTEXT), confirmed=True)
        updated = self.service.get(self.old.id)
        restored = self.service.recoverable_action(self.old.id, action="undo", confirmed=True, expected_version=updated.updated_at)
        self.assertEqual(restored.state(), self.old.state())
        self.assertEqual([h.action for h in restored.history], ["created", "task_updated", "undone"])
        self.assertEqual(restored.history[1].changes["notice"]["raw_notice"], FOLLOWUP)
        self.assertTrue(workflow.prepare(draft().source, CONTEXT).already_processed)
        log = "".join(p.read_text(encoding="utf-8") for p in (self.root / "logs").glob("*.jsonl"))
        self.assertIn("task_undone", log)
        with self.assertRaises(ValidationError): self.service.recoverable_action(self.old.id, action="undo", confirmed=True)

    def test_delete_requires_confirmation_and_can_be_undone(self):
        with self.assertRaises(ValidationError): self.service.recoverable_action(self.old.id, action="delete")
        deleted = self.service.recoverable_action(self.old.id, action="delete", confirmed=True)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(self.service.list(), [])
        self.assertEqual(len(self.service.list(include_deleted=True)), 1)
        restored = self.service.recoverable_action(self.old.id, action="undo", confirmed=True)
        self.assertIsNone(restored.deleted_at)
        self.assertEqual(len(self.service.list()), 1)

    def test_cancellation_notice_is_only_a_proposed_soft_delete(self):
        item = draft("【模拟数据】微观作业取消，不用交了。")
        output = relation([self.old.id], changes=(("delete", "作业取消"),), evidence="微观作业")
        workflow, _, _ = self.workflow(item, output)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertIn("deleted_at", plan.actions[0].diff)
        self.assertIsNone(self.service.get(self.old.id).deleted_at)
        workflow.apply(plan, confirmed=True)
        self.assertIsNotNone(self.service.get(self.old.id).deleted_at)

    def test_unknown_year_or_missing_sending_time_does_not_guess(self):
        item = draft(sent_at=None)
        workflow, _, _ = self.workflow(item)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "uncertain")
        selected = workflow.prepare(item.source, CONTEXT, target_id=self.old.id, due_override=Deadline.from_input("2026-11-27"))
        self.assertEqual(selected.actions[0].kind, "update")

    def test_year_context_is_converted_to_shanghai_before_month_day_proposal(self):
        self.service.set_deadline(self.old.id, Deadline.from_input("2027-01-25"))
        item = draft("【模拟数据】上次的微观作业延期到1月27日。", sent_at="2026-12-31T18:00:00+00:00")
        output = relation([self.old.id], changes=(("deadline", "延期到1月27日"),))
        workflow, _, _ = self.workflow(item, output)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "update")
        self.assertEqual(plan.actions[0].diff["deadline"]["after"]["date"], "2027-01-27")

    def test_old_message_never_silently_overwrites_newer_notice(self):
        item = draft(sent_at="2026-11-16T18:00:00+08:00")
        workflow, _, _ = self.workflow(item)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "uncertain")
        self.assertTrue(any("过时" in issue for issue in plan.actions[0].issues))

    def test_invalid_model_target_evidence_or_extra_commands_stay_uncertain(self):
        outputs = [relation(["not-an-id"]), relation([self.old.id], evidence="不存在的证据"), {**relation([self.old.id]), "execute": "delete_database"}]
        for output in outputs:
            workflow, _, _ = self.workflow(output=output)
            self.assertEqual(workflow.prepare(draft().source, CONTEXT).actions[0].kind, "uncertain")

    def test_missing_new_requirement_does_not_mark_batch_successful(self):
        output = relation([self.old.id], changes=(("submission.method", "其他要求不变"),))
        workflow, _, _ = self.workflow(output=output)
        plan = workflow.prepare(draft().source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "uncertain")

    def test_exact_duplicate_is_not_just_equal_keywords(self):
        changed_due = replace(self.old, deadline=Deadline.from_input("2026-11-27"), source=Notice(""))
        original_without_source = replace(self.old, source=Notice(""))
        self.assertIsNone(exact_duplicate(changed_due, [original_without_source]))

    def test_recurring_generic_notice_with_different_sending_date_not_auto_duplicate(self):
        first = draft("【模拟数据】明天交作业", sent_at="2026-11-17T18:00:00+08:00")
        second = replace(first, source=Notice(first.source.raw_notice, "2026-11-24T18:00:00+08:00"))
        old = Task.create(course=first.course, title=first.title, description=first.description, source=first.source)
        self.assertIsNone(exact_duplicate(second, [old]))

    def test_postpone_word_and_earlier_date_are_explicit_conflict(self):
        item = draft("【模拟数据】上次的微观作业延期到2026年11月23日。", due="2026-11-23")
        output = relation([self.old.id], changes=(("deadline", "延期到2026年11月23日"),))
        workflow, _, _ = self.workflow(item, output)
        plan = workflow.prepare(item.source, CONTEXT)
        self.assertEqual(plan.actions[0].kind, "uncertain")
        self.assertIn("提前", plan.actions[0].preview()["change_types"])
        self.assertTrue(any("冲突" in issue for issue in plan.actions[0].issues))

    def test_multiple_updates_to_same_old_task_are_conflict(self):
        first = draft()
        second = replace(first, title="另一修改片段", description="另一修改")
        workflow, _, client = self.workflow()
        workflow.parser = FakeParser([first, second])
        plan = workflow.prepare(first.source, CONTEXT)
        self.assertTrue(all(action.kind == "uncertain" for action in plan.actions))

    def test_two_distinct_assignments_in_one_message_are_both_preserved(self):
        raw = "【模拟数据】微观第1次作业和第2次作业分别提交，是两个独立任务。"
        first = draft(raw, title="第1次作业", due="2026-11-25")
        second = replace(first, title="第2次作业", deadline=Deadline.from_input("2026-11-27"))
        workflow, _, _ = self.workflow(first, relation([], kind="new", changes=(), evidence="两个独立任务"))
        workflow.parser = FakeParser([first, second])
        plan = workflow.prepare(first.source, CONTEXT)
        self.assertEqual([action.kind for action in plan.actions], ["new", "new"])
        result = workflow.apply(plan, confirmed=True)
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(self.service.list()), 3)

    def test_legacy_history_can_undo_manual_deadline_change(self):
        old_document = self.old.to_dict()
        updated = self.old.set_deadline(Deadline.from_input("2026-11-26"))
        legacy = updated.to_dict()
        legacy["history"][-1]["changes"] = {"deadline": {"before": old_document["deadline"], "after": legacy["deadline"]}}
        self.assertEqual(Task.from_dict(legacy).undo().deadline, self.old.deadline)

    def test_completed_at_and_updated_at_compatibility_and_undo_completion(self):
        completed = self.old.complete()
        self.assertEqual(completed.completed_at, completed.updated_at)
        self.assertEqual(completed.undo().status, "pending")

    def test_round_two_task_and_document_still_load(self):
        document = self.old.to_dict(); document.pop("deleted_at")
        task = Task.from_dict(document)
        self.assertIsNone(task.deleted_at)
        self.assertNotIn("processed_batches", self.service.document())


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_persistent_cache_prevents_repeated_call_after_cancel_or_restart(self):
        fake = FakeClient(response())
        factory_calls = []
        def factory(): factory_calls.append(1); return fake
        client = CachedClient(self.root, factory, PARSE_SCHEMA, stage="parse")
        first = client.complete("【模拟】prompt", {"untrusted_notice": "【模拟】明天交作业", "sent_at": SENT})
        second = CachedClient(self.root, factory, PARSE_SCHEMA, stage="parse").complete("【模拟】prompt", {"untrusted_notice": "【模拟】明天交作业", "sent_at": SENT})
        self.assertEqual(first, second)
        self.assertEqual(len(factory_calls), 1)

    def test_different_sent_time_or_context_invalidates_cache(self):
        fake = FakeClient(response())
        client = CachedClient(self.root, lambda: fake, PARSE_SCHEMA, stage="parse")
        for payload in ({"sent_at": SENT}, {"sent_at": "2026-11-19T18:00:00+08:00"}, {"sent_at": SENT, "course": "另一课程"}):
            client.complete("模拟", payload)
        self.assertEqual(len(fake.calls), 3)

    def test_corrupt_cache_does_not_silently_trigger_paid_call(self):
        fake = FakeClient(response())
        client = CachedClient(self.root, lambda: fake, PARSE_SCHEMA, stage="parse")
        client.complete("模拟", {})
        path = next((self.root / "cache" / "parse").glob("*.json")); path.write_text("{", encoding="utf-8")
        with self.assertRaises(StorageError): client.complete("模拟", {})
        self.assertEqual(len(fake.calls), 1)
        CachedClient(self.root, lambda: fake, PARSE_SCHEMA, stage="parse", refresh=True).complete("模拟", {})
        self.assertEqual(len(fake.calls), 2)

    def test_concurrent_cache_misses_only_make_one_call(self):
        from concurrent.futures import ThreadPoolExecutor
        fake = FakeClient(response())
        def run(_): return CachedClient(self.root, lambda: fake, PARSE_SCHEMA, stage="parse").complete("模拟", {})
        with ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(run, range(3)))
        self.assertEqual(len(fake.calls), 1)
