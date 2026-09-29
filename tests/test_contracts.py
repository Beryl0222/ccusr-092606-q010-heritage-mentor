import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heritage_mentor.contracts import validate_event
from heritage_mentor.service import Ledger, Policy
from heritage_mentor.state import ReducerError, replay
from heritage_mentor.store import EventStore, StoreError

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="hm-tests-"))


def make_ledger(name: str, policy: Policy | None = None) -> tuple[Ledger, Path]:
    schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
    path = _TMP_ROOT / name / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(EventStore(path, schema), policy=policy), path


def seed_basics(ledger: Ledger, *, grant_to: str | None = "2027-01-31T23:59:59+08:00") -> None:
    """协议+资质+授权+年级前提+安全/课程教师+费用政策。"""
    ledger.record_agreement("agr-1", "v1 初始协议", False, "2026-09-01T10:00:00+08:00")
    ledger.qualify_mentor(
        "M1", "agr-1", "剪纸", "2026-09-01T10:00:00+08:00",
        "2027-12-31T23:59:59+08:00", ["paper-cut-basic"], "2026-09-01T10:05:00+08:00",
    )
    ledger.grant_content(
        "grant-1", "M1", "agr-1", "paper-cut-basic",
        ["in_class", "public_show"], "2026-09-01T10:10:00+08:00", grant_to,
        "2026-09-01T10:10:00+08:00",
    )
    ledger.set_grades("series-1", ["G3", "G4"], "2026-09-02T09:00:00+08:00")
    ledger.assign_teacher(
        "series-1", "T-safety", "safety",
        "2026-09-01T00:00:00+08:00", None, "2026-09-02T09:05:00+08:00",
    )
    ledger.assign_teacher(
        "series-1", "T-curr", "curriculum",
        "2026-09-01T00:00:00+08:00", None, "2026-09-02T09:06:00+08:00",
    )
    ledger.set_fee_policy("series-1", 200.0, 50.0, "2026-09-02T09:10:00+08:00")


def plan_first(ledger: Ledger, lesson_id: str = "L1", **overrides) -> object:
    args = dict(
        lesson_id=lesson_id, series_id="series-1", mentor_id="M1", class_ref="C3-1",
        grade="G3", scheduled_at="2026-10-10T10:00:00+08:00",
        scheduled_end="2026-10-10T11:00:00+08:00",
        required_grants=["paper-cut-basic"], required_resources=["craft-room"],
        occurred_at="2026-09-20T09:00:00+08:00",
    )
    args.update(overrides)
    return ledger.plan_lesson(**args)


class ContractEnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_time_version_and_payload_required(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = {(i.field, i.code) for i in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_required_fields(self) -> None:
        event = dict(self.sample, event_type="MENTOR_QUALIFIED",
                     aggregate_type="mentor_profile", payload={})
        issues = validate_event(event, self.schema)
        fields = {i.field for i in issues}
        self.assertIn("payload.mentor_id", fields)
        self.assertIn("payload.valid_to", fields)

    def test_event_must_belong_to_aggregate(self) -> None:
        event = dict(self.sample, event_type="LESSON_PLANNED",
                     aggregate_type="partnership_agreement",
                     payload={"series_id": "s"})
        codes = {(i.field, i.code) for i in validate_event(event, self.schema)}
        self.assertIn(("aggregate_type", "event_not_allowed_for_aggregate"), codes)

    def test_payload_datetime_requires_timezone(self) -> None:
        event = dict(self.sample, event_type="CONTENT_GRANTED",
                     aggregate_type="content_grant",
                     payload={
                         "mentor_id": "M1", "agreement_id": "agr-1",
                         "content_ref": "x", "scope": ["in_class"],
                         "valid_from": "2026-09-01T10:00:00",
                         "valid_to": "2026-10-01T10:00:00+08:00",
                     })
        fields = {i.field for i in validate_event(event, self.schema)}
        self.assertIn("payload.valid_from", fields)


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = make_ledger(self._testMethodName)

    def test_full_prerequisites_open_the_lesson(self) -> None:
        seed_basics(self.ledger)
        decision = plan_first(self.ledger)
        self.assertTrue(decision.ok, decision.issues)

    def test_missing_each_prerequisite_blocks(self) -> None:
        ledger = self.ledger
        ledger.record_agreement("agr-1", "v1", False, "2026-09-01T10:00:00+08:00")
        ledger.qualify_mentor("M1", "agr-1", "剪纸", "2026-09-01T10:00:00+08:00", None,
                              ["paper-cut-basic"], "2026-09-01T10:05:00+08:00")
        ledger.grant_content("g1", "M1", "agr-1", "paper-cut-basic", ["in_class"],
                             "2026-09-01T10:10:00+08:00", None, "2026-09-01T10:10:00+08:00")
        # 无年级前提、无教师
        d = plan_first(ledger)
        self.assertFalse(d.ok)
        self.assertIn("grade_prerequisite_missing", d.issues)
        self.assertIn("safety_teacher_not_assigned", d.issues)
        self.assertIn("curriculum_teacher_not_assigned", d.issues)

        ledger.set_grades("series-1", ["G4"], "2026-09-02T09:00:00+08:00")
        ledger.assign_teacher("series-1", "T1", "safety",
                              "2026-09-01T00:00:00+08:00", None, "2026-09-02T09:05:00+08:00")
        d = plan_first(ledger)
        self.assertIn("grade_not_allowed", d.issues)

    def test_expired_grant_blocks_and_reschedule_revalidates(self) -> None:
        ledger = self.ledger
        seed_basics(ledger, grant_to="2026-10-05T23:59:59+08:00")  # 授权在 10/10 课前过期
        d = plan_first(ledger)
        self.assertFalse(d.ok)
        self.assertTrue(any(i.startswith("grant_expired:") for i in d.issues), d.issues)
        # 调课到授权有效期内即可排上
        d = plan_first(ledger, scheduled_at="2026-10-01T10:00:00+08:00",
                       scheduled_end="2026-10-01T11:00:00+08:00")
        self.assertTrue(d.ok, d.issues)
        # 再临时调回到期之后：旧内容不能继续使用
        d = ledger.reschedule_lesson(
            "L1", "2026-10-08T10:00:00+08:00", "2026-10-08T11:00:00+08:00",
            ["craft-room"], "2026-09-25T09:00:00+08:00",
        )
        self.assertFalse(d.ok)
        self.assertTrue(any(i.startswith("grant_expired:") for i in d.issues))

    def test_overbooking_mentor_and_resource(self) -> None:
        ledger = self.ledger
        seed_basics(ledger)
        self.assertTrue(plan_first(ledger, lesson_id="L1").ok)
        # 另一班级同一时段、同一导师 → 导师超额
        d = plan_first(ledger, lesson_id="L2", class_ref="C3-2",
                       required_resources=["other-room"])
        self.assertIn("mentor_overbooked", d.issues)
        # 同一专用设备 → 设备超额（即使换导师也不行）
        ledger.qualify_mentor("M2", "agr-1", "扎染", "2026-09-01T10:00:00+08:00",
                              None, ["tie-dye"], "2026-09-01T10:06:00+08:00")
        ledger.grant_content("g2", "M2", "agr-1", "tie-dye", ["in_class"],
                             "2026-09-01T10:10:00+08:00", None, "2026-09-01T10:11:00+08:00")
        d = ledger.plan_lesson(
            lesson_id="L3", series_id="series-1", mentor_id="M2", class_ref="C4-1",
            grade="G4", scheduled_at="2026-10-10T10:30:00+08:00",
            scheduled_end="2026-10-10T11:30:00+08:00",
            required_grants=["tie-dye"], required_resources=["craft-room"],
            occurred_at="2026-09-20T09:01:00+08:00",
        )
        self.assertIn("resource_busy", d.issues)
        self.assertNotIn("mentor_overbooked", d.issues)

    def test_reschedule_frees_old_block_and_allows_new_booking(self) -> None:
        ledger = self.ledger
        seed_basics(ledger)
        plan_first(ledger, lesson_id="L1")
        self.assertTrue(ledger.reschedule_lesson(
            "L1", "2026-10-11T10:00:00+08:00", "2026-10-11T11:00:00+08:00",
            ["craft-room"], "2026-09-25T09:00:00+08:00",
        ).ok)
        # 旧时段被释放，别的课可以占用 craft-room（需另一位有效导师）
        ledger.qualify_mentor("M2", "agr-1", "扎染", "2026-09-01T10:00:00+08:00",
                              None, ["tie-dye"], "2026-09-26T10:00:00+08:00")
        ledger.grant_content("g2", "M2", "agr-1", "tie-dye", ["in_class"],
                             "2026-09-26T10:00:00+08:00", None, "2026-09-26T10:01:00+08:00")
        d = ledger.plan_lesson(
            "L9", "series-1", "M2", "C4-2", "G4",
            "2026-10-10T10:00:00+08:00", "2026-10-10T11:00:00+08:00",
            ["tie-dye"], ["craft-room"], "2026-09-26T10:05:00+08:00",
        )
        self.assertTrue(d.ok, d.issues)


class DeliveryAndSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = make_ledger(self._testMethodName)
        seed_basics(self.ledger)
        plan_first(self.ledger)

    def _attendance(self, *, class_ref="C3-1", scheduled_at="2026-10-10T10:00:00+08:00",
                    students=("S1", "S2")) -> object:
        return self.ledger.record_attendance(
            "L1", class_ref, scheduled_at, list(students),
            "2026-10-10T11:05:00+08:00",
        )

    def test_duplicate_receipt_counts_once(self) -> None:
        d1 = self._attendance()
        d2 = self._attendance()  # 同键重发
        self.assertTrue(d1.ok and d2.ok and d2.idempotent)
        lesson = self.ledger.s.lesson("L1")
        self.assertEqual(1, len(lesson.receipts))

    def test_mismatched_receipt_pauses_settlement(self) -> None:
        self._attendance(class_ref="C3-9")  # 班级不一致
        self.ledger.record_artifact_receipt(
            "L1", "C3-1", "2026-10-10T10:00:00+08:00", 3,
            "2026-10-10T11:10:00+08:00",
        )
        d = self.ledger.confirm_delivery(
            "L1", ["paper-cut-basic"], {"mentor": "示范", "students": "练习"},
            "hash-1", "2026-10-10T11:15:00+08:00",
        )
        self.assertIn("attendance_class_mismatch", d.issues)
        holds = self.ledger.settlement_holds("L1")
        self.assertIn("attendance_class_mismatch", holds)
        pay = self.ledger.settle_fee("L1", "corr-1", "2026-10-20T10:00:00+08:00")
        self.assertFalse(pay.ok)
        self.assertIn("settlement_on_hold", pay.code)

    def test_content_without_grant_raises_rectify_and_holds_fee(self) -> None:
        # 已排课并签到，但交付确认中使用了从未授权的内容：
        # 交付事实仍记录，同时立整改义务，结算保持暂停。
        self._attendance()
        d = self.ledger.confirm_delivery(
            "L1", ["paper-cut-advanced"], {"mentor": "x"}, "h",
            "2026-10-10T11:15:00+08:00",
        )
        self.assertFalse(d.ok)
        self.assertEqual("delivery_with_infractions", d.code)
        self.assertTrue(any(i.startswith("content_used_without_grant:") for i in d.issues))
        self.assertTrue(self.ledger.s.open_obligations(context_ref="L1"))
        pay = self.ledger.settle_fee("L1", "corr-x", "2026-10-20T10:00:00+08:00")
        self.assertFalse(pay.ok)
        self.assertEqual("settlement_on_hold", pay.code)

    def test_happy_path_settlement_and_correlation_idempotency(self) -> None:
        self._attendance()
        self.ledger.record_artifact_receipt(
            "L1", "C3-1", "2026-10-10T10:00:00+08:00", 2,
            "2026-10-10T11:10:00+08:00",
        )
        self.ledger.confirm_delivery(
            "L1", ["paper-cut-basic"], {"mentor": "示范", "students": "S1,S2"},
            "hash-1", "2026-10-10T11:15:00+08:00",
        )
        self.assertEqual(200.0, self.ledger.amount_due("L1"))
        pay = self.ledger.settle_fee("L1", "corr-1", "2026-10-20T10:00:00+08:00")
        self.assertTrue(pay.ok, pay.issues)
        again = self.ledger.settle_fee("L1", "corr-1", "2026-10-20T10:00:00+08:00")
        self.assertTrue(again.idempotent)
        self.assertEqual(1, len(self.ledger.s.lesson("L1").settlements))

    def test_feedback_opens_rectify_obligation(self) -> None:
        self._attendance()
        self.ledger.confirm_delivery(
            "L1", ["paper-cut-basic"], {}, "h", "2026-10-10T11:15:00+08:00",
        )
        self.ledger.give_feedback("L1", ["safety_gap"], "2026-10-11T09:00:00+08:00")
        self.assertIn("open_obligation", self.ledger.settlement_holds("L1"))


class PublicationApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = make_ledger(self._testMethodName)
        seed_basics(self.ledger)
        plan_first(self.ledger)
        self.ledger.record_attendance("L1", "C3-1", "2026-10-10T10:00:00+08:00",
                                      ["S1", "S2"], "2026-10-10T11:05:00+08:00")
        self.ledger.confirm_delivery(
            "L1", ["paper-cut-basic"], {"mentor": "M1", "students": "S1,S2"},
            "hash-1", "2026-10-10T11:15:00+08:00",
        )
        self.ledger.grant_consent(
            "consent-S1", "S1", "C3-1", "guardian1@example",
            ["public_show"], "2026-09-15T00:00:00+08:00", None,
            "2026-09-15T08:00:00+08:00",
        )
        self.ledger.grant_consent(
            "consent-S2", "S2", "C3-1", "guardian2@example",
            ["public_show"], "2026-09-15T00:00:00+08:00", None,
            "2026-09-15T08:01:00+08:00",
        )

    def _publish(self, artifact_id="A1", fee_bearing=True) -> object:
        return self.ledger.publish_artifact(
            artifact_id, "L1", ["S1", "S2"], ["grant-1"],
            [{"party": "M1", "role": "工艺示范"}, {"party": "S1", "role": "制作"}],
            "public_show", fee_bearing, "2026-11-01T10:00:00+08:00",
        )

    def test_fee_bearing_requires_two_independent_approvals(self) -> None:
        d = self._publish()
        self.assertFalse(d.ok)
        self.assertIn("independent_approvals_incomplete", d.issues)
        self.ledger.record_approval("ap-m", "A1", "mentor", "M1", True,
                                    "2026-10-28T10:00:00+08:00")
        d = self._publish()
        self.assertIn("independent_approvals_incomplete", d.issues)
        # 同一批准人不能代表两方
        self.ledger.record_approval("ap-s", "A1", "school", "M1", True,
                                    "2026-10-28T11:00:00+08:00")
        d = self._publish()
        self.assertIn("approvals_not_independent", d.issues)

    def test_independent_approvals_pass_and_royalty_added(self) -> None:
        self.ledger.record_approval("ap-m", "A1", "mentor", "M1", True,
                                    "2026-10-28T10:00:00+08:00")
        self.ledger.record_approval("ap-s", "A1", "school", "principal-X", True,
                                    "2026-10-28T11:00:00+08:00")
        d = self._publish()
        self.assertTrue(d.ok, d.issues)
        self.assertEqual(250.0, self.ledger.amount_due("L1"))

    def test_non_fee_bearing_publication_needs_no_approvals(self) -> None:
        d = self._publish("A2", fee_bearing=False)
        self.assertTrue(d.ok, d.issues)
        self.assertEqual(200.0, self.ledger.amount_due("L1"))

    def test_consent_and_grant_windows_checked_at_publication(self) -> None:
        # 授权聚合 grant-1 出新版本，把窗口收窄到发布日之前
        self.ledger.grant_content(
            "grant-1", "M1", "agr-1", "paper-cut-basic", ["in_class", "public_show"],
            "2026-09-01T10:10:00+08:00", "2026-10-20T23:59:59+08:00",
            "2026-10-19T10:00:00+08:00",
        )
        d = self.ledger.publish_artifact(
            "A3", "L1", ["S1", "S2"], ["grant-1"], [],
            "public_show", False, "2026-11-01T10:00:00+08:00",
        )
        self.assertFalse(d.ok)
        self.assertIn("grant_expired:grant-1", d.issues)

    def test_missing_guardian_consent_blocks_publication(self) -> None:
        d = self.ledger.publish_artifact(
            "A4", "L1", ["S1", "S3"], ["grant-1"], [],
            "public_show", False, "2026-11-01T10:00:00+08:00",
        )
        self.assertFalse(d.ok)
        self.assertIn("consent_missing_or_expired:S3", d.issues)


class NarrowingAndExitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = make_ledger(self._testMethodName)
        seed_basics(self.ledger)
        plan_first(self.ledger)
        self.ledger.record_attendance("L1", "C3-1", "2026-10-10T10:00:00+08:00",
                                      ["S1"], "2026-10-10T11:05:00+08:00")
        self.ledger.confirm_delivery(
            "L1", ["paper-cut-basic"], {"mentor": "M1"}, "hash-1",
            "2026-10-10T11:15:00+08:00",
        )
        self.ledger.grant_consent(
            "c1", "S1", "C3-1", "g@example", ["public_show"],
            "2026-09-15T00:00:00+08:00", None, "2026-09-15T08:00:00+08:00",
        )
        self.ledger.publish_artifact(
            "A1", "L1", ["S1"], ["grant-1"],
            [{"party": "M1", "role": "示范"}], "public_show", False,
            "2026-10-15T10:00:00+08:00",
        )

    def test_narrowing_blocks_future_but_keeps_past_basis(self) -> None:
        ledger = self.ledger
        d = ledger.record_agreement("agr-1", "v2 收窄公开传播授权", True,
                                    "2026-10-20T10:00:00+08:00")
        self.assertTrue(d.ok)
        # 已完成的课仍可结算（原依据保留）
        self.assertEqual([], [h for h in ledger.settlement_holds("L1")])
        pay = ledger.settle_fee("L1", "corr-1", "2026-10-22T10:00:00+08:00")
        self.assertTrue(pay.ok, pay.issues)
        # 已公开成果自动产生下架/补署名义务
        obs = ledger.s.open_obligations(context_ref="artifact:A1")
        self.assertEqual(1, len(obs))
        # 未来新课被拦
        d = plan_first(ledger, lesson_id="L2", class_ref="C3-2",
                       scheduled_at="2026-11-10T10:00:00+08:00",
                       scheduled_end="2026-11-10T11:00:00+08:00",
                       required_resources=["other-room"])
        self.assertIn("agreement_narrowed", d.issues)
        # 新的公开使用被拦
        ledger.grant_consent("c2", "S1", "C3-1", "g@example", ["public_show"],
                             "2026-09-15T00:00:00+08:00", None, "2026-10-21T08:00:00+08:00")
        d = ledger.publish_artifact(
            "A2", "L1", ["S1"], ["grant-1"], [], "public_show", False,
            "2026-10-21T10:00:00+08:00",
        )
        self.assertIn("agreement_narrowed_for_new_use", d.issues)

    def test_exit_blocks_future_bookings_and_raises_handover(self) -> None:
        ledger = self.ledger
        d = ledger.exit_mentor("M1", "handover://notes-1", ["series-1"],
                               "2026-10-20T10:00:00+08:00")
        self.assertTrue(d.ok)
        self.assertTrue(ledger.s.open_obligations(context_ref="mentor:M1"))
        d = plan_first(ledger, lesson_id="L3", class_ref="C3-3",
                       scheduled_at="2026-11-10T10:00:00+08:00",
                       scheduled_end="2026-11-10T11:00:00+08:00",
                       required_resources=["room-x"])
        self.assertIn("mentor_exited", d.issues)


class PersistenceAndRestartTests(unittest.TestCase):
    def test_restart_replays_state_and_keeps_deadlines_anchored(self) -> None:
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        path = tmp / "ledger.jsonl"
        ledger = Ledger(EventStore(path, schema))
        seed_basics(ledger)
        plan_first(ledger)
        ledger.record_attendance("L1", "C3-1", "2026-10-10T10:00:00+08:00",
                                 ["S1"], "2026-10-10T11:05:00+08:00")
        ledger.confirm_delivery("L1", ["paper-cut-basic"], {}, "h",
                                "2026-10-10T11:15:00+08:00")
        before = ledger.amount_due("L1")

        replayed = Ledger(EventStore(path, schema))
        self.assertEqual(before, replayed.amount_due("L1"))
        # 课酬期限锚定交付事件时间，不随重启变化
        from heritage_mentor import projections
        rows = projections.deadlines_board(replayed)["deadlines"]
        payment = [r for r in rows if r["kind"] == "payment" and r["ref"] == "L1"]
        self.assertEqual(1, len(payment))
        self.assertEqual("2026-10-10T11:15:00+08:00", payment[0]["anchored_at"])
        self.assertTrue(payment[0]["due_at"].startswith("2026-10-25T11:15:00"))

    def test_duplicate_event_id_is_idempotent_on_replay(self) -> None:
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        path = tmp / "ledger.jsonl"
        store = EventStore(path, schema)
        event = {
            "event_id": "e1", "event_type": "AGREEMENT_VERSIONED",
            "aggregate_type": "partnership_agreement", "aggregate_id": "agr-x",
            "occurred_at": "2026-09-01T10:00:00+08:00", "version": 1,
            "payload": {"change_summary": "v1", "narrowed": False},
        }
        store.append(event)
        store.append(dict(event))  # 重发同一事件
        self.assertEqual(1, len(store.events()))

    def test_version_gap_is_rejected(self) -> None:
        event = {
            "event_id": "e1", "event_type": "AGREEMENT_VERSIONED",
            "aggregate_type": "partnership_agreement", "aggregate_id": "agr-y",
            "occurred_at": "2026-09-01T10:00:00+08:00", "version": 2,
            "payload": {"change_summary": "v1", "narrowed": False},
        }
        with self.assertRaises(ReducerError):
            replay([event])

    def test_bad_contract_event_rejected_by_store(self, ) -> None:
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        store = EventStore(tmp / "l.jsonl", schema)
        bad = {
            "event_id": "x", "event_type": "UNKNOWN",
            "aggregate_type": "partnership_agreement", "aggregate_id": "a",
            "occurred_at": "2026-09-01T10:00:00+08:00", "version": 1, "payload": {},
        }
        with self.assertRaises(StoreError):
            store.append(bad)


if __name__ == "__main__":
    unittest.main()
