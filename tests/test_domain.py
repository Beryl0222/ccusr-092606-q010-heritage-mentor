import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heritage_mentor.domain import (  # noqa: E402
    DomainError,
    LedgerState,
    decide,
    decide_and_apply,
    parse_dt,
    replay,
)
from heritage_mentor import views  # noqa: E402

T0 = "2026-09-01T09:00:00+08:00"


class LedgerBuilder:
    """构造一个具备协议/导师/授权/课程的最小可用状态。"""

    def __init__(self) -> None:
        self.state = LedgerState()
        self.seq = 0
        self.agreement("A1")
        self.mentor("M1")
        self.grant("G1", "M1", contents=["C1", "C2"], grades=["g3", "g4"])
        self.course("K1", "G1", "M1")

    def id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq:04d}"

    def run(self, command: dict) -> list[dict]:
        command.setdefault("event_id", self.id("evt"))
        command.setdefault("occurred_at", T0)
        return decide_and_apply(command, self.state)

    def agreement(self, ref: str) -> None:
        self.run({
            "type": "version_agreement", "agreement_ref": ref, "terms_version": 1,
            "effective_from": "2026-09-01T00:00:00+08:00", "status": "active",
        })

    def mentor(self, mid: str) -> None:
        self.run({"type": "register_mentor", "mentor_id": mid, "qualification": "q"})

    def grant(self, gid: str, mid: str, contents=("C1", "C2"), grades=("g3", "g4"),
              issued="2026-09-02T00:00:00+08:00", expires="2027-01-31T00:00:00+08:00",
              agreement="A1", scope=("teach", "demonstrate")) -> None:
        self.run({
            "type": "authorize_grant", "grant_id": gid, "mentor_id": mid, "agreement_ref": agreement,
            "scope": list(scope), "content_refs": list(contents),
            "grade_prerequisites": list(grades),
            "issued_at": issued, "expires_at": expires,
        })

    def course(self, cid: str, gid: str, mid: str, grade="g3", fee=500, quorum=1) -> None:
        self.run({
            "type": "plan_course", "course_id": cid, "mentor_id": mid, "grant_id": gid,
            "grade_level": grade, "collaborating_teacher_ids": ["T1"],
            "lesson_count": 2, "preparation_due_at": "2026-09-10T00:00:00+08:00",
            "quorum": quorum, "lesson_fee": fee,
        })
        self.run({
            "type": "confirm_preparation", "course_id": cid,
            "confirmed_at": "2026-09-09T10:00:00+08:00",
        })

    def schedule(self, lid: str, cid: str, mid: str, start="2026-09-15T10:00:00+08:00",
                 end="2026-09-15T11:00:00+08:00", resources=(), klass="3-1") -> None:
        self.run({
            "type": "schedule_lesson", "lesson_id": lid, "course_id": cid, "mentor_id": mid,
            "class_ref": klass, "starts_at": start, "ends_at": end,
            "resource_window": {"resources": [
                {"resource_id": r, "capacity": cap} for r, cap in resources
            ]},
        })


class SchedulingTests(unittest.TestCase):
    def test_mentor_cannot_be_double_booked(self) -> None:
        b = LedgerBuilder()
        b.schedule("L1", "K1", "M1")
        with self.assertRaises(DomainError) as ctx:
            b.schedule("L2", "K1", "M1", start="2026-09-15T10:30:00+08:00", end="2026-09-15T11:30:00+08:00")
        self.assertEqual("mentor_overbooked", ctx.exception.code)

    def test_dedicated_resource_capacity_is_enforced(self) -> None:
        b = LedgerBuilder()
        b.mentor("M2")
        b.grant("G2", "M2")
        b.course("K2", "G2", "M2")
        b.mentor("M3")
        b.grant("G3", "M3")
        b.course("K3", "G3", "M3")
        window = ("2026-09-15T10:00:00+08:00", "2026-09-15T11:00:00+08:00")
        b.schedule("L1", "K1", "M1", *window, resources=[("R", 2)])
        b.schedule("L2", "K2", "M2", *window, resources=[("R", 2)])
        with self.assertRaises(DomainError) as ctx:
            b.schedule("L3", "K3", "M3", *window, resources=[("R", 2)])
        self.assertEqual("resource_overbooked", ctx.exception.code)

    def test_non_overlapping_window_reuses_resource(self) -> None:
        b = LedgerBuilder()
        b.mentor("M2")
        b.grant("G2", "M2")
        b.course("K2", "G2", "M2")
        b.schedule("L1", "K1", "M1", resources=[("R", 1)])
        b.schedule("L2", "K2", "M2",
                   start="2026-09-15T11:00:00+08:00", end="2026-09-15T12:00:00+08:00",
                   resources=[("R", 1)])
        self.assertEqual(2, len(b.state.lessons))

    def test_reschedule_frees_old_window(self) -> None:
        b = LedgerBuilder()
        b.mentor("M2")
        b.grant("G2", "M2")
        b.course("K2", "G2", "M2")
        b.schedule("L1", "K1", "M1", resources=[("R", 1)])
        b.schedule("L2", "K2", "M2",
                   start="2026-09-15T11:00:00+08:00", end="2026-09-15T12:00:00+08:00",
                   resources=[("R", 1)])
        b.run({
            "type": "reschedule_lesson", "lesson_id": "L1",
            "starts_at": "2026-09-16T10:00:00+08:00", "ends_at": "2026-09-16T11:00:00+08:00",
            "resource_window": {"resources": [{"resource_id": "R", "capacity": 1}]},
            "reason": "校历调整",
        })
        # 旧窗口 09-15 10:00 释放，M2 可以平移到该窗口
        b.run({
            "type": "reschedule_lesson", "lesson_id": "L2",
            "starts_at": "2026-09-15T10:00:00+08:00", "ends_at": "2026-09-15T11:00:00+08:00",
            "resource_window": {"resources": [{"resource_id": "R", "capacity": 1}]},
            "reason": "导师时间调整",
        })

    def test_lesson_beyond_grant_window_is_rejected(self) -> None:
        b = LedgerBuilder()
        with self.assertRaises(DomainError) as ctx:
            b.schedule("L1", "K1", "M1",
                       start="2027-01-30T10:00:00+08:00", end="2027-02-02T11:00:00+08:00")
        self.assertEqual("grant_window_lapsed", ctx.exception.code)

    def test_grade_prerequisite_and_teacher_required(self) -> None:
        b = LedgerBuilder()
        with self.assertRaises(DomainError) as ctx:
            b.course("KBAD", "G1", "M1", grade="g9")
        self.assertEqual("grade_prerequisite_failed", ctx.exception.code)
        b.seq += 1
        with self.assertRaises(DomainError) as ctx:
            b.run({
                "type": "plan_course", "course_id": "KNOTEACHER", "mentor_id": "M1", "grant_id": "G1",
                "grade_level": "g3", "collaborating_teacher_ids": [],
                "lesson_count": 1, "preparation_due_at": "2026-09-10T00:00:00+08:00",
            })
        self.assertEqual("teacher_required", ctx.exception.code)


class DeliveryAndNarrowingTests(unittest.TestCase):
    def _delivered(self, contents=("C1",), at="2026-09-15T11:00:00+08:00") -> LedgerBuilder:
        b = LedgerBuilder()
        b.schedule("L1", "K1", "M1")
        b.run({
            "type": "confirm_delivery", "lesson_id": "L1",
            "contribution_snapshot": [{"party_ref": "M1", "role": "mentor", "share": 1.0}],
            "content_refs_used": list(contents), "evidence_hash": "h",
            "delivered_at": at,
        })
        return b

    def test_expired_or_removed_content_blocks_delivery(self) -> None:
        b = LedgerBuilder()
        b.grant("G9", "M1", expires="2027-01-30T11:00:00+08:00")
        b.course("K9", "G9", "M1")
        b.schedule("L9", "K9", "M1",
                   start="2027-01-30T10:00:00+08:00", end="2027-01-30T11:00:00+08:00")
        with self.assertRaises(DomainError) as ctx:
            b.run({
                "type": "confirm_delivery", "lesson_id": "L9",
                "contribution_snapshot": [{"party_ref": "M1", "role": "mentor"}],
                "content_refs_used": ["C1"], "evidence_hash": "h",
                "delivered_at": "2027-01-30T11:00:00+08:00",
            })
        self.assertEqual("content_not_licensed", ctx.exception.code)

    def test_narrowing_blocks_only_future_use(self) -> None:
        b = self._delivered()  # 已完成课次
        b.run({
            "type": "narrow_grant", "grant_id": "G1", "revised_scope": ["teach"],
            "removed_content_refs": ["C1"], "effective_from": "2026-10-01T00:00:00+08:00",
            "follow_up_obligations": [],
        })
        # 收窄生效前的未来课次仍可排；但授课时使用被移除内容将被拦截
        b.schedule("L2", "K1", "M1",
                   start="2026-10-02T10:00:00+08:00", end="2026-10-02T11:00:00+08:00")
        with self.assertRaises(DomainError) as ctx:
            b.run({
                "type": "confirm_delivery", "lesson_id": "L2",
                "contribution_snapshot": [{"party_ref": "M1", "role": "mentor"}],
                "content_refs_used": ["C1"], "evidence_hash": "h2",
                "delivered_at": "2026-10-02T11:00:00+08:00",
            })
        self.assertEqual("content_not_licensed", ctx.exception.code)
        # 已完成课次保留原依据（仍为 delivered，授权依据快照不变）
        self.assertEqual("delivered", b.state.lessons["L1"]["status"])
        self.assertEqual("2027-01-31T00:00:00+08:00", b.state.lessons["L1"]["grant_basis"]["expires_at"])

    def test_narrowing_raises_takedown_for_published_artifact(self) -> None:
        b = self._setup_published()
        events = b.run({
            "type": "narrow_grant", "grant_id": "G1", "revised_scope": ["teach"],
            "removed_content_refs": ["C1"], "effective_from": "2026-10-01T00:00:00+08:00",
            "follow_up_obligations": [
                {"type": "handoff", "handoff_id": "HO1", "items": ["教案"],
                 "due_at": "2026-10-10T00:00:00+08:00"},
            ],
        })
        kinds = {e["event_type"] for e in events}
        self.assertIn("ARTIFACT_TAKEDOWN_REQUESTED", kinds)
        self.assertIn("HANDOFF_REQUIRED", kinds)
        self.assertEqual("grant_narrowed", b.state.artifacts["ART1"]["takedown"]["reason"])
        self.assertIn("HO1", b.state.handoffs)

    def _setup_published(self) -> LedgerBuilder:
        b = self._delivered()
        b.run({
            "type": "grant_consent", "consent_id": "CS1", "student_ref": "S1", "guardian_ref": "P1",
            "scope": ["participation", "publication"],
            "granted_at": "2026-09-10T00:00:00+08:00", "valid_until": "2027-07-31T00:00:00+08:00",
        })
        b.run({
            "type": "register_artifact", "artifact_id": "ART1", "lesson_id": "L1",
            "student_refs": ["S1"], "source_content_refs": ["C1"],
            "produced_at": "2026-09-15T11:00:00+08:00",
        })
        b.run({
            "type": "decide_publication", "artifact_id": "ART1", "decision": "approved",
            "approver_role": "school_affairs", "approver_id": "U-affairs",
            "decided_at": "2026-09-16T09:00:00+08:00",
        })
        return b


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.b = LedgerBuilder()
        self.b.schedule("L1", "K1", "M1")
        self.b.run({
            "type": "confirm_delivery", "lesson_id": "L1",
            "contribution_snapshot": [{"party_ref": "M1", "role": "mentor"}],
            "content_refs_used": ["C1"], "evidence_hash": "h",
            "delivered_at": "2026-09-15T11:00:00+08:00",
        })
        self.b.run({
            "type": "grant_consent", "consent_id": "CS1", "student_ref": "S1", "guardian_ref": "P1",
            "scope": ["participation", "publication"],
            "granted_at": "2026-09-10T00:00:00+08:00", "valid_until": "2027-07-31T00:00:00+08:00",
        })
        self.b.run({
            "type": "register_artifact", "artifact_id": "ART1", "lesson_id": "L1",
            "student_refs": ["S1"], "source_content_refs": ["C1"],
        })

    def test_publication_requires_school_affairs_role(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.b.run({
                "type": "decide_publication", "artifact_id": "ART1", "decision": "approved",
                "approver_role": "finance_bursar", "approver_id": "U1",
                "decided_at": "2026-09-16T09:00:00+08:00",
            })
        self.assertEqual("approver_role_invalid", ctx.exception.code)

    def test_publication_blocked_without_consent(self) -> None:
        self.b.run({"type": "withdraw_consent", "consent_id": "CS1",
                    "withdrawn_at": "2026-09-15T20:00:00+08:00"})
        with self.assertRaises(DomainError) as ctx:
            self.b.run({
                "type": "decide_publication", "artifact_id": "ART1", "decision": "approved",
                "approver_role": "school_affairs", "approver_id": "U1",
                "decided_at": "2026-09-16T09:00:00+08:00",
            })
        self.assertEqual("consent_missing", ctx.exception.code)

    def test_fee_requires_prior_publication(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.b.run({
                "type": "decide_fee", "artifact_id": "ART1", "decision": "approved",
                "approver_role": "finance_bursar", "approver_id": "U2",
                "decided_at": "2026-09-16T09:00:00+08:00",
            })
        self.assertEqual("publication_not_approved", ctx.exception.code)

    def test_two_approvals_must_be_independent_persons(self) -> None:
        self.b.run({
            "type": "decide_publication", "artifact_id": "ART1", "decision": "approved",
            "approver_role": "school_affairs", "approver_id": "U-same",
            "decided_at": "2026-09-16T09:00:00+08:00",
        })
        with self.assertRaises(DomainError) as ctx:
            self.b.run({
                "type": "decide_fee", "artifact_id": "ART1", "decision": "approved",
                "approver_role": "finance_bursar", "approver_id": "U-same",
                "decided_at": "2026-09-16T10:00:00+08:00",
            })
        self.assertEqual("approvals_not_independent", ctx.exception.code)

    def test_decisions_are_immutable(self) -> None:
        self.b.run({
            "type": "decide_publication", "artifact_id": "ART1", "decision": "approved",
            "approver_role": "school_affairs", "approver_id": "U1",
            "decided_at": "2026-09-16T09:00:00+08:00",
        })
        with self.assertRaises(DomainError) as ctx:
            self.b.run({
                "type": "decide_publication", "artifact_id": "ART1", "decision": "rejected",
                "approver_role": "school_affairs", "approver_id": "U1",
                "decided_at": "2026-09-16T11:00:00+08:00",
            })
        self.assertEqual("decision_immutable", ctx.exception.code)

    def test_withdrawn_consent_triggers_takedown(self) -> None:
        self.b.run({
            "type": "decide_publication", "artifact_id": "ART1", "decision": "approved",
            "approver_role": "school_affairs", "approver_id": "U1",
            "decided_at": "2026-09-16T09:00:00+08:00",
        })
        events = self.b.run({"type": "withdraw_consent", "consent_id": "CS1",
                             "withdrawn_at": "2026-09-17T08:00:00+08:00"})
        self.assertIn("ARTIFACT_TAKEDOWN_REQUESTED", {e["event_type"] for e in events})
        self.assertEqual("consent_withdrawn", self.b.state.artifacts["ART1"]["takedown"]["reason"])


class ReceiptAndSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.b = LedgerBuilder()
        self.b.schedule("L1", "K1", "M1")
        self.b.run({
            "type": "confirm_delivery", "lesson_id": "L1",
            "contribution_snapshot": [{"party_ref": "M1", "role": "mentor"}],
            "content_refs_used": ["C1"], "evidence_hash": "h",
            "delivered_at": "2026-09-15T11:00:00+08:00",
        })
        self.receipt = {
            "receipt_ref": "RC1", "student_ref": "S1",
            "recorded_at": "2026-09-15T10:05:00+08:00", "fingerprint": "f1",
        }

    def _attendance(self, receipts, key=None, eid=None) -> list[dict]:
        return self.b.run({
            "type": "record_attendance", "lesson_id": "L1",
            "idempotency_key": key, "event_id": eid or self.b.id("att"),
            "receipts": receipts,
        })

    def test_duplicate_send_counts_once(self) -> None:
        cmd = {
            "type": "record_attendance", "lesson_id": "L1", "idempotency_key": "k1",
            "event_id": "att-fixed", "receipts": [dict(self.receipt)],
            "occurred_at": T0,
        }
        first = decide_and_apply(cmd, self.b.state)
        events_before = len(self.b.state.events)
        second = decide_and_apply(cmd, self.b.state)
        self.assertEqual(1, len(first))
        self.assertEqual(1, len(second))  # 决策回显首次事件
        self.assertEqual(events_before, len(self.b.state.events))  # 不重复归约
        self.assertEqual(1, len(self.b.state.lessons["L1"]["attendance"]))

    def test_duplicate_inside_one_batch_counts_once(self) -> None:
        events = self._attendance([dict(self.receipt), dict(self.receipt)])
        recorded = next(e for e in events if e["event_type"] == "ATTENDANCE_RECORDED")
        self.assertEqual(1, len(recorded["payload"]["receipts"]))

    def test_mismatched_receipt_places_hold(self) -> None:
        self._attendance([dict(self.receipt)], key="k1", eid="att-1")
        changed = dict(self.receipt, student_ref="S2")
        events = self._attendance([changed], eid="att-2")
        self.assertIn("SETTLEMENT_HOLD_PLACED", {e["event_type"] for e in events})
        self.assertTrue(self.b.state.active_holds("L1"))

    def test_hold_blocks_and_clearing_resumes_settlement(self) -> None:
        self.b.run({"type": "give_feedback", "lesson_id": "L1", "verdict": "pass",
                    "given_at": "2026-09-16T09:00:00+08:00"})
        self._attendance([dict(self.receipt)], key="k1", eid="att-1")
        self._attendance([dict(self.receipt, fingerprint="different")], eid="att-2")
        with self.assertRaises(DomainError) as ctx:
            self.b.run({"type": "raise_lesson_fee", "lesson_id": "L1", "obligation_id": "OB1",
                        "amount": 500, "due_at": "2026-10-15T00:00:00+08:00"})
        self.assertEqual("fee_conditions_unmet", ctx.exception.code)
        hold = self.b.state.active_holds("L1")[0]
        self.b.run({"type": "resolve_hold", "hold_id": hold["hold_id"],
                    "resolution": "核对无误", "cleared_at": "2026-09-16T12:00:00+08:00"})
        self.b.run({"type": "raise_lesson_fee", "lesson_id": "L1", "obligation_id": "OB1",
                    "amount": 500, "due_at": "2026-10-15T00:00:00+08:00"})
        self.b.run({"type": "settle_payout", "obligation_id": "OB1",
                    "settled_at": "2026-09-20T00:00:00+08:00", "amount": 500})
        self.assertEqual("2026-09-20T00:00:00+08:00", self.b.state.obligations["OB1"]["settled_at"])

    def test_payout_cannot_double_spend(self) -> None:
        self.b.run({"type": "give_feedback", "lesson_id": "L1", "verdict": "pass",
                    "given_at": "2026-09-16T09:00:00+08:00"})
        self._attendance([dict(self.receipt)], key="k1", eid="att-1")
        self.b.run({"type": "raise_lesson_fee", "lesson_id": "L1", "obligation_id": "OB1",
                    "amount": 500, "due_at": "2026-10-15T00:00:00+08:00"})
        self.b.run({"type": "settle_payout", "obligation_id": "OB1",
                    "settled_at": "2026-09-20T00:00:00+08:00", "amount": 500})
        with self.assertRaises(DomainError) as ctx:
            self.b.run({"type": "settle_payout", "obligation_id": "OB1",
                        "settled_at": "2026-09-21T00:00:00+08:00", "amount": 500})
        self.assertEqual("obligation_already_settled", ctx.exception.code)

    def test_quality_and_rectification_gate_fee(self) -> None:
        self._attendance([dict(self.receipt)], key="k1", eid="att-1")
        self.b.run({"type": "give_feedback", "lesson_id": "L1", "verdict": "conditional",
                    "given_at": "2026-09-16T09:00:00+08:00",
                    "rectification": {"rectification_id": "RC1",
                                      "due_at": "2026-09-23T00:00:00+08:00"}})
        with self.assertRaises(DomainError) as ctx:
            self.b.run({"type": "raise_lesson_fee", "lesson_id": "L1", "obligation_id": "OB1",
                        "amount": 500, "due_at": "2026-10-15T00:00:00+08:00"})
        self.assertEqual("fee_conditions_unmet", ctx.exception.code)
        self.b.run({"type": "resolve_rectification", "rectification_id": "RC1",
                    "resolved_at": "2026-09-20T00:00:00+08:00"})
        self.b.run({"type": "give_feedback", "lesson_id": "L1", "verdict": "pass",
                    "given_at": "2026-09-20T12:00:00+08:00"})
        self.b.run({"type": "raise_lesson_fee", "lesson_id": "L1", "obligation_id": "OB1",
                    "amount": 500, "due_at": "2026-10-15T00:00:00+08:00"})


class AttributionTests(unittest.TestCase):
    def test_attribution_must_cover_every_contributor(self) -> None:
        b = LedgerBuilder()
        b.schedule("L1", "K1", "M1")
        b.run({
            "type": "confirm_delivery", "lesson_id": "L1",
            "contribution_snapshot": [
                {"party_ref": "M1", "role": "mentor", "share": 0.6},
                {"party_ref": "T1", "role": "collaborating_teacher", "share": 0.2},
            ],
            "content_refs_used": ["C1"], "evidence_hash": "h",
            "delivered_at": "2026-09-15T11:00:00+08:00",
        })
        b.run({"type": "grant_consent", "consent_id": "CS1", "student_ref": "S1", "guardian_ref": "P1",
               "scope": ["participation", "publication"],
               "granted_at": "2026-09-10T00:00:00+08:00", "valid_until": "2027-07-31T00:00:00+08:00"})
        b.run({"type": "register_artifact", "artifact_id": "ART1", "lesson_id": "L1",
               "student_refs": ["S1"], "source_content_refs": ["C1"]})
        with self.assertRaises(DomainError) as ctx:
            b.run({"type": "supplement_attribution", "artifact_id": "ART1",
                   "attributions": [{"party_ref": "M1", "label": "导师"}]})
        self.assertEqual("attribution_incomplete", ctx.exception.code)


class DeadlinePersistenceTests(unittest.TestCase):
    def test_deadlines_survive_replay_without_restarting(self) -> None:
        b = LedgerBuilder()
        b.schedule("L1", "K1", "M1")
        b.run({
            "type": "confirm_delivery", "lesson_id": "L1",
            "contribution_snapshot": [{"party_ref": "M1", "role": "mentor"}],
            "content_refs_used": ["C1"], "evidence_hash": "h",
            "delivered_at": "2026-09-15T11:00:00+08:00",
        })
        b.run({"type": "give_feedback", "lesson_id": "L1", "verdict": "pass",
               "given_at": "2026-09-16T09:00:00+08:00"})
        b.run({"type": "record_attendance", "lesson_id": "L1",
               "receipts": [{"receipt_ref": "RC1", "student_ref": "S1",
                             "recorded_at": "2026-09-15T10:05:00+08:00", "fingerprint": "f"}]})
        b.run({"type": "raise_lesson_fee", "lesson_id": "L1", "obligation_id": "OB1",
               "amount": 500, "due_at": "2026-10-15T00:00:00+08:00"})
        b.run({
            "type": "narrow_grant", "grant_id": "G1", "revised_scope": ["teach"],
            "removed_content_refs": ["C2"], "effective_from": "2026-10-01T00:00:00+08:00",
            "follow_up_obligations": [
                {"type": "handoff", "handoff_id": "HO1", "items": ["x"],
                 "due_at": "2026-10-10T00:00:00+08:00"},
            ],
        })
        original = b.state
        restored = replay(original.events)  # 模拟服务重启
        self.assertEqual(original.obligations["OB1"]["due_at"], restored.obligations["OB1"]["due_at"])
        self.assertEqual(
            original.obligations["OB1"]["deadline_anchor_event"],
            restored.obligations["OB1"]["deadline_anchor_event"],
        )
        self.assertEqual(original.handoffs["HO1"]["due_at"], restored.handoffs["HO1"]["due_at"])
        self.assertEqual(original.grants["G1"]["expires_at"], restored.grants["G1"]["expires_at"])
        register = views.deadline_register(restored, parse_dt("2026-09-29T00:00:00+08:00"))
        kinds = {item["status"]: item["anchor_event"] for item in register["items"]}
        self.assertIn("payout", kinds)
        self.assertIn("handoff", kinds)
        self.assertIn("grant_expiry", kinds)
        # 重放不产生新的事件标识或版本
        self.assertEqual([e["event_id"] for e in original.events],
                         [e["event_id"] for e in restored.events])


class ViewTests(unittest.TestCase):
    def _full(self) -> LedgerBuilder:
        b = LedgerBuilder()
        b.schedule("L1", "K1", "M1")
        b.run({"type": "grant_consent", "consent_id": "CS1", "student_ref": "S1", "guardian_ref": "P1",
               "scope": ["participation", "publication"],
               "granted_at": "2026-09-10T00:00:00+08:00", "valid_until": "2027-07-31T00:00:00+08:00"})
        b.run({
            "type": "confirm_delivery", "lesson_id": "L1",
            "contribution_snapshot": [{"party_ref": "M1", "role": "mentor", "share": 0.8}],
            "content_refs_used": ["C1"], "evidence_hash": "h",
            "delivered_at": "2026-09-15T11:00:00+08:00",
        })
        b.run({"type": "record_attendance", "lesson_id": "L1",
               "receipts": [{"receipt_ref": "RC1", "student_ref": "S1",
                             "recorded_at": "2026-09-15T10:05:00+08:00", "fingerprint": "f"}]})
        b.run({"type": "give_feedback", "lesson_id": "L1", "verdict": "pass",
               "given_at": "2026-09-16T09:00:00+08:00"})
        b.run({"type": "register_artifact", "artifact_id": "ART1", "lesson_id": "L1",
               "student_refs": ["S1"], "source_content_refs": ["C1"]})
        return b

    def test_readiness_reflects_blockers(self) -> None:
        b = LedgerBuilder()
        b.run({"type": "schedule_lesson", "lesson_id": "LF", "course_id": "K1", "mentor_id": "M1",
               "class_ref": "3-1", "starts_at": "2026-09-15T10:00:00+08:00",
               "ends_at": "2026-09-15T11:00:00+08:00", "resource_window": {"resources": []}})
        view = views.course_readiness(b.state, "K1", parse_dt("2026-09-20T00:00:00+08:00"))
        lesson = next(l for l in view["lessons"] if l["lesson_id"] == "LF")
        self.assertIn("lesson_past_due_not_delivered", lesson["blockers"])
        self.assertFalse(view["ready_to_open"])

    def test_guardian_sees_only_own_child(self) -> None:
        b = self._full()
        b.run({"type": "grant_consent", "consent_id": "CS2", "student_ref": "S2", "guardian_ref": "P2",
               "scope": ["participation"], "granted_at": "2026-09-10T00:00:00+08:00",
               "valid_until": "2027-07-31T00:00:00+08:00"})
        view = views.guardian_view(b.state, "P1", parse_dt("2026-09-20T00:00:00+08:00"))
        self.assertEqual(["S1"], view["students"])
        # 未公开的成果不会出现在监护人视图
        self.assertEqual([], view["published_artifacts"])
        all_consents = {c["consent_id"] for c in view["consents"]}
        self.assertEqual({"CS1"}, all_consents)

    def test_provenance_traces_lesson_license_and_contributors(self) -> None:
        b = self._full()
        b.run({"type": "decide_publication", "artifact_id": "ART1", "decision": "approved",
               "approver_role": "school_affairs", "approver_id": "U1",
               "decided_at": "2026-09-17T09:00:00+08:00"})
        provenance = views.artifact_provenance(b.state, "ART1", parse_dt("2026-09-20T00:00:00+08:00"))
        self.assertEqual("L1", provenance["lesson"]["lesson_id"])
        self.assertEqual("G1", provenance["license"]["grant_id"])
        contributors = {c["party_ref"] for c in provenance["contributors"]}
        self.assertEqual({"M1", "S1"}, contributors)
        self.assertTrue(provenance["student_consents"]["S1"]["active"])

    def test_mentor_statement_totals_settled_and_payable(self) -> None:
        b = self._full()
        b.run({"type": "raise_lesson_fee", "lesson_id": "L1", "obligation_id": "OB1",
               "amount": 500, "due_at": "2026-10-15T00:00:00+08:00"})
        statement = views.mentor_statement(b.state, "M1", parse_dt("2026-09-20T00:00:00+08:00"))
        self.assertEqual(500, statement["totals"]["payable"])
        self.assertEqual(0, statement["totals"]["settled"])
        self.assertIn("L1", {l["lesson_id"] for l in statement["lessons"]})


if __name__ == "__main__":
    unittest.main()
