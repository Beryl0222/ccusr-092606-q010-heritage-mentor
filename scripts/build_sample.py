"""生成可直接重放的联调样例日志 data/journal.sample.jsonl。

覆盖：授权与年级前提、多班级容量排期、签到幂等与不一致暂停、
监护同意撤回、公开/费用双独立批准、补充署名、质量整改、
课酬与额外课酬、协议收窄后的下架与交接义务。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heritage_mentor.domain import DomainError  # noqa: E402
from heritage_mentor.store import EventStore  # noqa: E402

SCHEMA = ROOT / "contracts/domain.schema.json"
JOURNAL = ROOT / "data/journal.sample.jsonl"


def main() -> int:
    if JOURNAL.exists():
        JOURNAL.unlink()
    store = EventStore(JOURNAL, json.loads(SCHEMA.read_text(encoding="utf-8")))

    def apply(command: dict) -> None:
        try:
            store.append_decision(command)
        except DomainError as error:
            raise SystemExit(f"样例构造失败 @{command.get('type')}: {error.code} {error.message}")

    # 合作协议
    apply({
        "type": "version_agreement", "event_id": "evt-0001",
        "agreement_ref": "AGR-2026-09", "terms_version": 1,
        "effective_from": "2026-09-01T00:00:00+08:00", "status": "active",
        "occurred_at": "2026-09-01T09:00:00+08:00",
    })

    # 导师资质
    apply({
        "type": "register_mentor", "event_id": "evt-0002",
        "mentor_id": "MTR-chen", "qualification": "省级非物质文化遗产剪纸代表性传承人",
        "occurred_at": "2026-09-01T10:00:00+08:00",
    })
    apply({
        "type": "register_mentor", "event_id": "evt-0003",
        "mentor_id": "MTR-zhao", "qualification": "市级扎染技艺传承人",
        "occurred_at": "2026-09-01T10:05:00+08:00",
    })

    # 授权范围与期限（含年级前提）
    apply({
        "type": "authorize_grant", "event_id": "evt-0010",
        "grant_id": "GRN-chen-01", "mentor_id": "MTR-chen", "agreement_ref": "AGR-2026-09",
        "scope": ["teach", "demonstrate", "record"],
        "content_refs": ["CNT-jianzhi-basic", "CNT-jianzhi-advanced"],
        "grade_prerequisites": ["grade-3", "grade-4"],
        "issued_at": "2026-09-02T00:00:00+08:00",
        "expires_at": "2027-01-31T00:00:00+08:00",
        "occurred_at": "2026-09-02T10:00:00+08:00",
    })
    apply({
        "type": "authorize_grant", "event_id": "evt-0011",
        "grant_id": "GRN-zhao-01", "mentor_id": "MTR-zhao", "agreement_ref": "AGR-2026-09",
        "scope": ["teach", "demonstrate"],
        "content_refs": ["CNT-zaran-basic"],
        "grade_prerequisites": ["grade-3"],
        "issued_at": "2026-09-02T00:00:00+08:00",
        "expires_at": "2026-12-31T00:00:00+08:00",
        "occurred_at": "2026-09-02T10:05:00+08:00",
    })

    # 课程（协作教师承担课堂安全与课程目标）
    apply({
        "type": "plan_course", "event_id": "evt-0020",
        "course_id": "CRS-jianzhi-1", "mentor_id": "MTR-chen", "grant_id": "GRN-chen-01",
        "grade_level": "grade-3", "collaborating_teacher_ids": ["TCH-li"],
        "lesson_count": 4, "preparation_due_at": "2026-09-12T00:00:00+08:00",
        "quorum": 1, "lesson_fee": 500,
        "occurred_at": "2026-09-03T10:00:00+08:00",
    })
    apply({
        "type": "plan_course", "event_id": "evt-0021",
        "course_id": "CRS-zaran-1", "mentor_id": "MTR-zhao", "grant_id": "GRN-zhao-01",
        "grade_level": "grade-3", "collaborating_teacher_ids": ["TCH-li"],
        "lesson_count": 2, "preparation_due_at": "2026-09-12T00:00:00+08:00",
        "quorum": 1, "lesson_fee": 450,
        "occurred_at": "2026-09-03T10:05:00+08:00",
    })

    # 备课在期限前完成
    apply({
        "type": "confirm_preparation", "event_id": "evt-0022",
        "course_id": "CRS-jianzhi-1", "confirmed_at": "2026-09-10T16:00:00+08:00",
        "occurred_at": "2026-09-10T16:00:00+08:00",
    })
    apply({
        "type": "confirm_preparation", "event_id": "evt-0023",
        "course_id": "CRS-zaran-1", "confirmed_at": "2026-09-11T11:00:00+08:00",
        "occurred_at": "2026-09-11T11:00:00+08:00",
    })

    # 多班级同窗口预约同一导师与专用设备：容量 2，两个不同导师各一班可以并存
    apply({
        "type": "schedule_lesson", "event_id": "evt-0030",
        "lesson_id": "LSN-0915-01", "course_id": "CRS-jianzhi-1", "mentor_id": "MTR-chen",
        "class_ref": "CLS-3-1",
        "starts_at": "2026-09-15T10:00:00+08:00", "ends_at": "2026-09-15T11:00:00+08:00",
        "resource_window": {"resources": [{"resource_id": "RS-workbench-a", "capacity": 2}]},
        "occurred_at": "2026-09-04T09:00:00+08:00",
    })
    apply({
        "type": "schedule_lesson", "event_id": "evt-0031",
        "lesson_id": "LSN-0915-02", "course_id": "CRS-zaran-1", "mentor_id": "MTR-zhao",
        "class_ref": "CLS-3-2",
        "starts_at": "2026-09-15T10:00:00+08:00", "ends_at": "2026-09-15T11:00:00+08:00",
        "resource_window": {"resources": [{"resource_id": "RS-workbench-a", "capacity": 2}]},
        "occurred_at": "2026-09-04T09:05:00+08:00",
    })
    apply({
        "type": "schedule_lesson", "event_id": "evt-0032",
        "lesson_id": "LSN-0922-01", "course_id": "CRS-jianzhi-1", "mentor_id": "MTR-chen",
        "class_ref": "CLS-3-1",
        "starts_at": "2026-09-22T10:00:00+08:00", "ends_at": "2026-09-22T11:00:00+08:00",
        "resource_window": {"resources": []},
        "occurred_at": "2026-09-04T09:10:00+08:00",
    })

    # 监护同意
    apply({
        "type": "grant_consent", "event_id": "evt-0040",
        "consent_id": "CST-301", "student_ref": "STU-301", "guardian_ref": "GRD-301",
        "scope": ["participation", "publication"],
        "granted_at": "2026-09-10T00:00:00+08:00", "valid_until": "2027-07-31T00:00:00+08:00",
        "occurred_at": "2026-09-10T08:00:00+08:00",
    })
    apply({
        "type": "grant_consent", "event_id": "evt-0041",
        "consent_id": "CST-302", "student_ref": "STU-302", "guardian_ref": "GRD-302",
        "scope": ["participation", "publication"],
        "granted_at": "2026-09-10T00:00:00+08:00", "valid_until": "2027-07-31T00:00:00+08:00",
        "occurred_at": "2026-09-10T08:05:00+08:00",
    })

    # 剪纸课授课确认：贡献快照 + 示范内容 + 证据
    apply({
        "type": "confirm_delivery", "event_id": "evt-0050",
        "lesson_id": "LSN-0915-01",
        "contribution_snapshot": [
            {"party_ref": "MTR-chen", "role": "mentor", "share": 0.6},
            {"party_ref": "TCH-li", "role": "collaborating_teacher", "share": 0.2},
        ],
        "content_refs_used": ["CNT-jianzhi-basic"],
        "evidence_hash": "sha256:9f2c1a",
        "delivered_at": "2026-09-15T11:00:00+08:00",
        "occurred_at": "2026-09-15T11:05:00+08:00",
    })

    # 签到：正常回执 + 同一回执重复发送（幂等键，只计一次）
    attendance = {
        "type": "record_attendance", "event_id": "evt-0051", "lesson_id": "LSN-0915-01",
        "idempotency_key": "att-LSN-0915-01-301",
        "receipts": [{
            "receipt_ref": "RCP-att-001", "student_ref": "STU-301",
            "recorded_at": "2026-09-15T10:05:00+08:00", "fingerprint": "fp-301",
        }],
        "occurred_at": "2026-09-15T11:10:00+08:00",
    }
    apply(attendance)
    apply(attendance)  # 重复发送，只计算一次

    # 同一回执编号但参与人不一致：暂停结算
    apply({
        "type": "record_attendance", "event_id": "evt-0052",
        "lesson_id": "LSN-0915-01",
        "receipts": [{
            "receipt_ref": "RCP-att-001", "student_ref": "STU-302",
            "recorded_at": "2026-09-15T10:05:00+08:00", "fingerprint": "fp-301",
        }],
        "occurred_at": "2026-09-15T11:12:00+08:00",
    })

    # 扎染课授课 + 有条件通过的质量反馈 + 整改闭环
    apply({
        "type": "confirm_delivery", "event_id": "evt-0053",
        "lesson_id": "LSN-0915-02",
        "contribution_snapshot": [
            {"party_ref": "MTR-zhao", "role": "mentor", "share": 0.6},
            {"party_ref": "TCH-li", "role": "collaborating_teacher", "share": 0.2},
        ],
        "content_refs_used": ["CNT-zaran-basic"],
        "evidence_hash": "sha256:77ab02",
        "delivered_at": "2026-09-15T11:00:00+08:00",
        "occurred_at": "2026-09-15T11:05:00+08:00",
    })
    apply({
        "type": "record_attendance", "event_id": "evt-0054", "lesson_id": "LSN-0915-02",
        "idempotency_key": "att-LSN-0915-02-302",
        "receipts": [{
            "receipt_ref": "RCP-att-002", "student_ref": "STU-302",
            "recorded_at": "2026-09-15T10:05:00+08:00", "fingerprint": "fp-302",
        }],
        "occurred_at": "2026-09-15T11:10:00+08:00",
    })
    apply({
        "type": "give_feedback", "event_id": "evt-0060",
        "lesson_id": "LSN-0915-02", "verdict": "conditional", "given_at": "2026-09-16T09:00:00+08:00",
        "rectification": {
            "rectification_id": "RCT-0001", "due_at": "2026-09-23T00:00:00+08:00",
        },
        "occurred_at": "2026-09-16T09:30:00+08:00",
    })
    apply({
        "type": "resolve_rectification", "event_id": "evt-0061",
        "rectification_id": "RCT-0001", "resolved_at": "2026-09-20T15:00:00+08:00",
        "occurred_at": "2026-09-20T15:00:00+08:00",
    })
    apply({
        "type": "give_feedback", "event_id": "evt-0062",
        "lesson_id": "LSN-0915-02", "verdict": "pass", "given_at": "2026-09-20T16:00:00+08:00",
        "occurred_at": "2026-09-20T16:10:00+08:00",
    })

    # 剪纸课质量通过；暂停事项核对后闭环
    apply({
        "type": "give_feedback", "event_id": "evt-0063",
        "lesson_id": "LSN-0915-01", "verdict": "pass", "given_at": "2026-09-16T09:00:00+08:00",
        "occurred_at": "2026-09-16T09:30:00+08:00",
    })
    state = store.state
    hold = next(h for h in state.active_holds("LSN-0915-01") if h["reason"] == "receipt_mismatch")
    apply({
        "type": "resolve_hold", "event_id": "evt-0064",
        "hold_id": hold["hold_id"],
        "resolution": "教务核对签到原件，重复回执系录入笔误，以首次签到为准",
        "cleared_at": "2026-09-16T14:00:00+08:00",
        "occurred_at": "2026-09-16T14:00:00+08:00",
    })

    # 课酬条件齐备：生成义务并支付
    apply({
        "type": "raise_lesson_fee", "event_id": "evt-0070",
        "lesson_id": "LSN-0915-01", "obligation_id": "FEE-0001", "amount": 500,
        "due_at": "2026-10-15T00:00:00+08:00",
        "occurred_at": "2026-09-16T15:00:00+08:00",
    })
    apply({
        "type": "raise_lesson_fee", "event_id": "evt-0071",
        "lesson_id": "LSN-0915-02", "obligation_id": "FEE-0002", "amount": 450,
        "due_at": "2026-10-15T00:00:00+08:00",
        "occurred_at": "2026-09-21T09:00:00+08:00",
    })
    apply({
        "type": "settle_payout", "event_id": "evt-0072",
        "obligation_id": "FEE-0001", "settled_at": "2026-09-25T00:00:00+08:00", "amount": 500,
        "occurred_at": "2026-09-25T09:30:00+08:00",
    })

    # 学生成果登记（剪纸课）
    apply({
        "type": "register_artifact", "event_id": "evt-0080",
        "artifact_id": "ART-0001", "lesson_id": "LSN-0915-01",
        "student_refs": ["STU-301"], "source_content_refs": ["CNT-jianzhi-basic"],
        "receipt_ref": "RCP-art-001", "produced_at": "2026-09-15T11:00:00+08:00",
        "occurred_at": "2026-09-16T10:00:00+08:00",
    })
    # 公开传播批准（教务角色）与费用批准（财务角色，且为不同自然人）相互独立
    apply({
        "type": "decide_publication", "event_id": "evt-0081",
        "artifact_id": "ART-0001", "decision": "approved",
        "approver_role": "school_affairs", "approver_id": "USR-wang",
        "decided_at": "2026-09-17T09:00:00+08:00",
        "occurred_at": "2026-09-17T09:00:00+08:00",
    })
    apply({
        "type": "decide_fee", "event_id": "evt-0082",
        "artifact_id": "ART-0001", "decision": "approved",
        "approver_role": "finance_bursar", "approver_id": "USR-qian",
        "decided_at": "2026-09-17T10:00:00+08:00",
        "occurred_at": "2026-09-17T10:00:00+08:00",
    })
    # 费用产生前补齐全部贡献署名
    apply({
        "type": "supplement_attribution", "event_id": "evt-0083",
        "artifact_id": "ART-0001",
        "attributions": [
            {"party_ref": "MTR-chen", "label": "剪纸工艺示范与指导导师"},
            {"party_ref": "TCH-li", "label": "校内协作教师"},
            {"party_ref": "STU-301", "label": "学生创作者"},
        ],
        "occurred_at": "2026-09-17T11:00:00+08:00",
    })
    apply({
        "type": "raise_artifact_fee", "event_id": "evt-0084",
        "artifact_id": "ART-0001", "obligation_id": "FEE-0003", "amount": 200,
        "due_at": "2026-10-20T00:00:00+08:00",
        "occurred_at": "2026-09-17T13:00:00+08:00",
    })
    apply({
        "type": "settle_payout", "event_id": "evt-0085",
        "obligation_id": "FEE-0003", "settled_at": "2026-09-26T00:00:00+08:00", "amount": 200,
        "occurred_at": "2026-09-26T09:30:00+08:00",
    })

    # 扎染成果公开后，监护人撤回同意：立即产生下架义务并完成下架
    apply({
        "type": "register_artifact", "event_id": "evt-0090",
        "artifact_id": "ART-0002", "lesson_id": "LSN-0915-02",
        "student_refs": ["STU-302"], "source_content_refs": ["CNT-zaran-basic"],
        "receipt_ref": "RCP-art-002", "produced_at": "2026-09-15T11:00:00+08:00",
        "occurred_at": "2026-09-17T10:00:00+08:00",
    })
    apply({
        "type": "decide_publication", "event_id": "evt-0091",
        "artifact_id": "ART-0002", "decision": "approved",
        "approver_role": "school_affairs", "approver_id": "USR-wang",
        "decided_at": "2026-09-18T09:00:00+08:00",
        "occurred_at": "2026-09-18T09:00:00+08:00",
    })
    apply({
        "type": "withdraw_consent", "event_id": "evt-0092",
        "consent_id": "CST-302", "withdrawn_at": "2026-09-19T18:00:00+08:00",
        "occurred_at": "2026-09-19T18:05:00+08:00",
    })
    apply({
        "type": "complete_takedown", "event_id": "evt-0093",
        "artifact_id": "ART-0002", "completed_at": "2026-09-20T12:00:00+08:00",
        "occurred_at": "2026-09-20T12:00:00+08:00",
    })

    # 授权收窄：自 10-01 起移除基础工艺的演示授权。
    # 只阻止未来课次与新用途；已完成的 LSN-0915-01 保留原依据，
    # 已公开使用该内容的 ART-0001 产生下架义务，同时产生资料交接义务。
    apply({
        "type": "narrow_grant", "event_id": "evt-0100",
        "grant_id": "GRN-chen-01", "revised_scope": ["teach"],
        "removed_content_refs": ["CNT-jianzhi-basic", "CNT-jianzhi-advanced"],
        "effective_from": "2026-10-01T00:00:00+08:00",
        "follow_up_obligations": [
            {"type": "handoff", "handoff_id": "HOF-0001",
             "items": ["剪纸教案", "材料与工具清单", "安全注意事项"],
             "due_at": "2026-10-10T00:00:00+08:00"},
        ],
        "occurred_at": "2026-09-28T09:00:00+08:00",
    })

    print(f"wrote {len(store.state.events)} events -> {JOURNAL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
