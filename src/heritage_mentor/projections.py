"""角色投影：同一事件流上的只读视图。

- 教务视图：课次是否具备开课条件、阻断项与关键期限；
- 导师视图：贡献记录与应付/已付费用；
- 监护人视图：仅返回与本人孩子相关的授权与成果（最小可见）；
- 成果溯源：来自哪次课、采用何种授权、各方贡献、批准与未完成交接。

所有期限均由事件时间推导，重建状态后结果一致，与进程重启无关。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from . import timeutil
from .service import Ledger, Policy


def _iso(dt: Any) -> str:
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def academic_view(ledger: Ledger, *, as_of: str | None = None) -> dict[str, Any]:
    """教务开课看板：每个课次的开课条件、收窄/退出影响与交接义务。"""
    moment = timeutil.parse(as_of) if as_of else timeutil.now()
    lessons_out: list[dict[str, Any]] = []
    for lesson_id, lesson in ledger.s.lessons.items():
        if not lesson.plan:
            continue
        plan = lesson.plan["payload"]
        if lesson.delivery:
            blockers: list[str] = []
        else:
            blockers = ledger.readiness(
                plan, at=plan["scheduled_at"], exclude_lesson=lesson_id
            )
        lessons_out.append({
            "lesson_id": lesson_id,
            "series_id": plan["series_id"],
            "class_ref": plan["class_ref"],
            "grade": plan["grade"],
            "mentor_id": plan["mentor_id"],
            "scheduled_at": plan["scheduled_at"],
            "scheduled_end": plan["scheduled_end"],
            "status": "delivered" if lesson.delivery else (
                "ready" if not blockers else "blocked"
            ),
            "blockers": blockers,
            "required_resources": list(plan.get("required_resources", [])),
            "open_obligations": [
                ob["payload"]["kind"] for ob in ledger.s.open_obligations(context_ref=lesson_id)
            ],
        })
    lessons_out.sort(key=lambda row: row["scheduled_at"])
    return {"as_of": _iso(moment), "lessons": lessons_out}


def mentor_view(ledger: Ledger, mentor_id: str) -> dict[str, Any]:
    """导师视图：核对自己的授课、贡献快照与每笔课酬。"""
    sessions: list[dict[str, Any]] = []
    total_due = 0.0
    total_settled = 0.0
    for lesson_id, lesson in ledger.s.lessons.items():
        if not lesson.plan or lesson.plan["payload"]["mentor_id"] != mentor_id:
            continue
        holds = ledger.settlement_holds(lesson_id) if lesson.delivery else ["delivery_missing"]
        amount = None
        if lesson.delivery and not holds:
            try:
                amount = ledger.amount_due(lesson_id)
                total_due += amount
            except Exception:  # noqa: BLE001 - 缺费用政策时按 None 展示
                amount = None
        settled = sum(float(e["payload"]["amount"]) for e in lesson.settlements)
        total_settled += settled
        row = {
            "lesson_id": lesson_id,
            "series_id": lesson.plan["payload"]["series_id"],
            "class_ref": lesson.plan["payload"]["class_ref"],
            "scheduled_at": lesson.plan["payload"]["scheduled_at"],
            "delivered_at": lesson.delivery["occurred_at"] if lesson.delivery else None,
            "content_used": list(lesson.delivery["payload"]["content_used"]) if lesson.delivery else [],
            "contribution_snapshot": (
                dict(lesson.delivery["payload"]["contribution_snapshot"]) if lesson.delivery else None
            ),
            "settlement_holds": holds,
            "amount_due": amount,
            "amount_settled": round(settled, 2),
        }
        sessions.append(row)
    sessions.sort(key=lambda row: row["scheduled_at"])
    mentor = ledger.s.mentors.get(mentor_id)
    return {
        "mentor_id": mentor_id,
        "exited": bool(mentor and mentor.get("exit")),
        "sessions": sessions,
        "total_due": round(total_due, 2),
        "total_settled": round(total_settled, 2),
        "balance_unpaid": round(total_due - total_settled, 2),
    }


def guardian_view(ledger: Ledger, student_id: str) -> dict[str, Any]:
    """监护人最小可见视图：只含自己孩子的授权与孩子署名的成果。

    刻意不返回：其他学生名单、他人监护人联系方式、费用、内部义务细节。
    """
    consents_out: list[dict[str, Any]] = []
    for ev in ledger.s.consents.get(student_id, []):
        body = ev["payload"]
        consents_out.append({
            "consent_id": ev["aggregate_id"],
            "class_ref": body["class_ref"],
            "scope": list(body["scope"]),
            "valid_from": body["valid_from"],
            "valid_to": body["valid_to"],
            "recorded_at": ev["occurred_at"],
        })
    artifacts_out: list[dict[str, Any]] = []
    for artifact_id, record in ledger.s.artifacts.items():
        published = record.get("published")
        if not published or student_id not in published["payload"]["student_ids"]:
            continue
        body = published["payload"]
        lesson = ledger.s.lesson(body["lesson_id"])
        artifacts_out.append({
            "artifact_id": artifact_id,
            "lesson_id": body["lesson_id"],
            "class_ref": lesson.plan["payload"]["class_ref"] if lesson and lesson.plan else None,
            "venue": body["venue"],
            "published_at": body["published_at"],
            "delisted": record.get("delisted") is not None,
        })
    return {
        "student_id": student_id,
        "consents": consents_out,
        "artifacts": artifacts_out,
    }


def artifact_lineage(ledger: Ledger, artifact_id: str) -> dict[str, Any]:
    """公开成果溯源：课次、授权、贡献、批准、下架与交接义务全貌。"""
    record = ledger.s.artifacts.get(artifact_id)
    if record is None or record.get("published") is None:
        return {"artifact_id": artifact_id, "found": False}
    published = record["published"]
    body = published["payload"]
    lesson = ledger.s.lesson(body["lesson_id"])
    plan = lesson.plan["payload"] if lesson and lesson.plan else None
    grants_out: list[dict[str, Any]] = []
    for grant_id in body["grants_used"]:
        history = ledger.s.grants.get(grant_id, [])
        if not history:
            grants_out.append({"grant_id": grant_id, "status": "unknown"})
            continue
        g = history[-1]["payload"]
        grants_out.append({
            "grant_id": grant_id,
            "mentor_id": g["mentor_id"],
            "agreement_id": g["agreement_id"],
            "content_ref": g["content_ref"],
            "scope": list(g["scope"]),
            "valid_from": g["valid_from"],
            "valid_to": g["valid_to"],
        })
    approvals_out: list[dict[str, Any]] = []
    for ev in ledger.s.approvals.values():
        b = ev["payload"]
        if b["artifact_ref"] == artifact_id:
            approvals_out.append({
                "approval_id": ev["aggregate_id"],
                "channel": b["channel"],
                "approver_id": b["approver_id"],
                "granted": b["granted"],
                "granted_at": b["granted_at"],
            })
    approvals_out.sort(key=lambda row: row["channel"])
    obligations_out = [
        {
            "obligation_id": oid,
            "kind": ob["raised"]["payload"]["kind"],
            "due_at": ob["raised"]["payload"]["due_at"],
            "context_ref": ob["raised"]["payload"]["context_ref"],
            "closed": ob["closed"] is not None,
        }
        for oid, ob in ledger.s.obligations.items()
        if ob.get("raised") and (
            ob["raised"]["payload"].get("context_ref") == f"artifact:{artifact_id}"
            or (plan and ob["raised"]["payload"].get("context_ref") == body["lesson_id"])
        )
    ]
    return {
        "artifact_id": artifact_id,
        "found": True,
        "lesson": {
            "lesson_id": body["lesson_id"],
            "series_id": plan["series_id"] if plan else None,
            "class_ref": plan["class_ref"] if plan else None,
            "scheduled_at": plan["scheduled_at"] if plan else None,
            "delivered_at": lesson.delivery["occurred_at"] if lesson and lesson.delivery else None,
            "evidence_hash": (
                lesson.delivery["payload"]["evidence_hash"] if lesson and lesson.delivery else None
            ),
        },
        "grants": grants_out,
        "student_ids": list(body["student_ids"]),
        "contributors": [dict(c) for c in body["contributors"]],
        "venue": body["venue"],
        "fee_bearing": body["fee_bearing"],
        "approvals": approvals_out,
        "published_at": body["published_at"],
        "delisted": record.get("delisted") is not None,
        "delist_reason": (
            record["delisted"]["payload"]["reason"] if record.get("delisted") else None
        ),
        "open_obligations": [o for o in obligations_out if not o["closed"]],
        "obligations": obligations_out,
    }


def deadlines_board(ledger: Ledger, policy: Policy | None = None) -> dict[str, Any]:
    """期限看板：备课/授权/整改/课酬/交接，锚点均为事件时间。"""
    policy = policy or ledger.policy
    rows: list[dict[str, Any]] = []

    # 备课：以最近一次排期事件为锚，开课前 prep_days 为备课截止
    for lesson_id, lesson in ledger.s.lessons.items():
        if not lesson.plan or lesson.delivery:
            continue
        anchor = lesson.plan["occurred_at"]
        scheduled = lesson.plan["payload"]["scheduled_at"]
        prep_due = (timeutil.parse(scheduled) - timedelta(days=policy.prep_days)).isoformat()
        rows.append({
            "kind": "prep",
            "ref": lesson_id,
            "anchor_event_id": lesson.plan["event_id"],
            "anchored_at": anchor,
            "due_at": prep_due,
            "class_opens_at": scheduled,
            "grace_note": f"需在开课前 {policy.prep_days} 天完成备课",
        })

    # 授权：未交付课次依赖的授权到期日
    pending = [
        (lid, ls) for lid, ls in ledger.s.lessons.items()
        if ls.plan and not ls.delivery
    ]
    for lesson_id, lesson in pending:
        for content_ref in lesson.plan["payload"]["required_grants"]:
            for grant in ledger.s.grants_for(lesson.plan["payload"]["mentor_id"], content_ref):
                body = grant["payload"]
                if body.get("valid_to"):
                    rows.append({
                        "kind": "grant_expiry",
                        "ref": f"{lesson_id}:{content_ref}",
                        "anchor_event_id": grant["event_id"],
                        "anchored_at": grant["occurred_at"],
                        "due_at": body["valid_to"],
                        "grant_id": grant["aggregate_id"],
                    })

    # 整改 / 下架补署名 / 交接义务
    for oid, ob in ledger.s.obligations.items():
        raised = ob.get("raised")
        if not raised or ob.get("closed"):
            continue
        body = raised["payload"]
        rows.append({
            "kind": body["kind"],
            "ref": oid,
            "anchor_event_id": raised["event_id"],
            "anchored_at": raised["occurred_at"],
            "due_at": body["due_at"],
            "context_ref": body.get("context_ref"),
        })

    # 课酬：交付后 payment_days 到期；暂停结算时附阻断项
    for lesson_id, lesson in ledger.s.lessons.items():
        if not lesson.delivery or lesson.settlements:
            continue
        holds = ledger.settlement_holds(lesson_id)
        due = (timeutil.parse(lesson.delivery["occurred_at"])
               + timedelta(days=policy.payment_days)).isoformat()
        rows.append({
            "kind": "payment",
            "ref": lesson_id,
            "anchor_event_id": lesson.delivery["event_id"],
            "anchored_at": lesson.delivery["occurred_at"],
            "due_at": due,
            "holds": holds,
        })

    rows.sort(key=lambda row: (row["due_at"], row["kind"]))
    return {"deadlines": rows}
