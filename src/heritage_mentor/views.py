"""面向不同角色的只读投影。

所有视图都从事件重放后的状态计算，不持有独立于日志的事实，
因此服务重启后视图与期限都可还原。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .domain import (
    SCOPE_PUBLICATION,
    LedgerState,
    _consent_covers,
    _grant_covers,
    _published_artifacts_with_student,
    _student_consent,
    parse_dt,
)


def _holds(state: LedgerState, ref: str) -> list[dict[str, Any]]:
    return [
        {"reason": h["reason"], "detected_at": h["detected_at"], "details": h.get("details", {})}
        for h in state.active_holds(ref)
    ]


def _deadline(status: str, due_at: str, anchor_event: str, now: datetime) -> dict[str, Any]:
    return {
        "status": status,
        "due_at": due_at,
        "anchor_event": anchor_event,
        "overdue": parse_dt(due_at) < now,
    }


def _lesson_readiness(state: LedgerState, lesson: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    course = state.courses[lesson["course_id"]]
    grant = state.grants[course["grant_id"]]
    starts_at = parse_dt(lesson["starts_at"])
    blockers: list[str] = []
    if not course.get("preparation_confirmed_at"):
        blockers.append("preparation_not_confirmed")
    if starts_at >= now and course.get("preparation_due_at") and now > parse_dt(course["preparation_due_at"]) and not course.get("preparation_confirmed_at"):
        blockers.append("preparation_overdue")
    if parse_dt(grant["expires_at"]) < parse_dt(lesson["ends_at"]):
        blockers.append("grant_expires_before_lesson_end")
    if lesson["status"] == "scheduled" and now > starts_at:
        blockers.append("lesson_past_due_not_delivered")
    if state.active_holds(lesson["lesson_id"]):
        blockers.append("settlement_on_hold")
    return {
        "lesson_id": lesson["lesson_id"],
        "status": lesson["status"],
        "starts_at": lesson["starts_at"],
        "ends_at": lesson["ends_at"],
        "delivered": lesson["status"] == "delivered",
        "blockers": blockers,
        "ready": not blockers,
    }


def course_readiness(state: LedgerState, course_id: str, now: datetime) -> dict[str, Any]:
    """教务视图：课程是否具备开课/结算条件，逐项给出原因。"""
    course = state.courses.get(course_id)
    if course is None:
        raise KeyError(course_id)
    grant = state.grants[course["grant_id"]]
    agreement = state.agreements[course["agreement_ref"]]
    lessons = [state.lessons[lid] for lid in course["lessons"]]
    lesson_views = [_lesson_readiness(state, lesson, now) for lesson in lessons]
    blockers: list[str] = []
    if agreement["status"] != "active":
        blockers.append("agreement_not_active")
    if now > parse_dt(grant["expires_at"]):
        blockers.append("grant_expired")
    if grant.get("narrowed_at") and now >= parse_dt(grant["narrowed_at"]) and not grant.get("content_refs_after_narrowing"):
        blockers.append("grant_scope_empty")
    if not course.get("preparation_confirmed_at"):
        blockers.append("preparation_not_confirmed")
    scheduled = [l for l in lessons if l["status"] == "scheduled"]
    delivered = [l for l in lessons if l["status"] == "delivered"]
    return {
        "course_id": course_id,
        "mentor_id": course["mentor_id"],
        "grade_level": course["grade_level"],
        "collaborating_teacher_ids": list(course["collaborating_teacher_ids"]),
        "agreement": {
            "agreement_ref": course["agreement_ref"],
            "terms_version": agreement["terms_version"],
            "status": agreement["status"],
        },
        "grant": {
            "grant_id": grant["grant_id"],
            "scope": list(grant["scope"]),
            "content_refs": sorted(grant["content_refs"]),
            "expires_at": grant["expires_at"],
            "narrowed_at": grant.get("narrowed_at"),
        },
        "preparation": {
            "confirmed": bool(course.get("preparation_confirmed_at")),
            "confirmed_at": course.get("preparation_confirmed_at"),
            "due_at": course["preparation_due_at"],
            "overdue": not course.get("preparation_confirmed_at") and now > parse_dt(course["preparation_due_at"]),
        },
        "lessons": lesson_views,
        "scheduled_count": len(scheduled),
        "delivered_count": len(delivered),
        "blockers": blockers,
        "ready_to_open": not blockers and all(view["ready"] for view in lesson_views),
    }


def mentor_statement(state: LedgerState, mentor_id: str, now: datetime) -> dict[str, Any]:
    """导师视图：可核对自己的贡献与应付/已付费用。"""
    lessons = [
        lesson
        for lesson in state.lessons.values()
        if lesson["mentor_id"] == mentor_id
    ]
    lesson_views = []
    for lesson in sorted(lessons, key=lambda item: item["starts_at"]):
        lesson_views.append(
            {
                "lesson_id": lesson["lesson_id"],
                "status": lesson["status"],
                "starts_at": lesson["starts_at"],
                "contribution_snapshot": lesson.get("contribution_snapshot", []),
                "content_refs_used": lesson.get("content_refs_used", []),
                "evidence_hash": lesson.get("evidence_hash"),
                "quality": lesson.get("quality"),
                "holds": _holds(state, lesson["lesson_id"]),
            }
        )
    obligations = [
        obligation
        for obligation in state.obligations.values()
        if obligation["mentor_id"] == mentor_id
    ]
    obligation_views = []
    totals = {"payable": 0, "settled": 0, "held": 0}
    for obligation in sorted(obligations, key=lambda item: item["due_at"]):
        settled = bool(obligation.get("settled_at"))
        held = bool(state.active_holds(obligation["source_ref"]))
        view = {
            "obligation_id": obligation["obligation_id"],
            "kind": obligation["kind"],
            "amount": obligation["amount"],
            "source_ref": obligation["source_ref"],
            "due_at": obligation["due_at"],
            "deadline_anchor_event": obligation["deadline_anchor_event"],
            "settled_at": obligation.get("settled_at"),
            "held": held and not settled,
        }
        obligation_views.append(view)
        if settled:
            totals["settled"] += obligation["amount"]
        elif held:
            totals["held"] += obligation["amount"]
        else:
            totals["payable"] += obligation["amount"]
    handoffs = [
        {
            "handoff_id": handoff["handoff_id"],
            "items": list(handoff["items"]),
            "due_at": handoff["due_at"],
            "deadline_anchor_event": handoff["deadline_anchor_event"],
            "completed_at": handoff.get("completed_at"),
            "pending": [item for item in handoff["items"] if item not in handoff.get("completed_items", [])],
        }
        for handoff in state.handoffs.values()
        if handoff["mentor_id"] == mentor_id
    ]
    return {
        "mentor_id": mentor_id,
        "qualification": state.mentors.get(mentor_id, {}).get("qualification"),
        "lessons": lesson_views,
        "obligations": obligation_views,
        "totals": totals,
        "handoffs": handoffs,
    }


def guardian_view(state: LedgerState, guardian_ref: str, now: datetime) -> dict[str, Any]:
    """监护人视图：只呈现与自己孩子相关的授权和成果，不含他人信息。"""
    students = sorted(
        {
            consent["student_ref"]
            for consent in state.consents.values()
            if consent["guardian_ref"] == guardian_ref
        }
    )
    consent_views = []
    for consent in state.consents.values():
        if consent["guardian_ref"] != guardian_ref:
            continue
        consent_views.append(
            {
                "consent_id": consent["consent_id"],
                "student_ref": consent["student_ref"],
                "scope": list(consent["scope"]),
                "granted_at": consent["granted_at"],
                "valid_until": consent["valid_until"],
                "withdrawn_at": consent.get("withdrawn_at"),
                "active": _consent_covers(state, consent, "participation", now)
                or _consent_covers(state, consent, "publication", now),
            }
        )
    artifact_views = []
    for student_ref in students:
        for artifact in _published_artifacts_with_student(state, student_ref, now):
            lesson = state.lessons[artifact["lesson_id"]]
            course = state.courses[lesson["course_id"]]
            grant = state.grants[course["grant_id"]]
            artifact_views.append(
                {
                    "artifact_id": artifact["artifact_id"],
                    "student_ref": student_ref,
                    "lesson_id": artifact["lesson_id"],
                    "source_content_refs": list(artifact["source_content_refs"]),
                    "license": {
                        "grant_id": grant["grant_id"],
                        "scope": list(grant["scope"]),
                        "content_refs": sorted(grant["content_refs"]),
                        "expires_at": grant["expires_at"],
                    },
                    "publication": artifact.get("publication"),
                    "takedown": artifact.get("takedown"),
                    "attributions": artifact.get("attributions", []),
                }
            )
    return {
        "guardian_ref": guardian_ref,
        "students": students,
        "consents": consent_views,
        "published_artifacts": artifact_views,
    }


def artifact_provenance(state: LedgerState, artifact_id: str, now: datetime) -> dict[str, Any]:
    """任一公开成果的完整溯源：来自哪次课、授权范围、贡献与未完成交接。"""
    artifact = state.artifacts.get(artifact_id)
    if artifact is None:
        raise KeyError(artifact_id)
    lesson = state.lessons[artifact["lesson_id"]]
    course = state.courses[lesson["course_id"]]
    grant = state.grants[course["grant_id"]]
    agreement = state.agreements[course["agreement_ref"]]
    invalid_content = _grant_covers(state, grant, now, artifact["source_content_refs"])
    consents = {}
    for student_ref in artifact["student_refs"]:
        consent = _student_consent(state, student_ref, SCOPE_PUBLICATION, now)
        consents[student_ref] = {
            "consent_id": consent["consent_id"] if consent else None,
            "guardian_ref": consent["guardian_ref"] if consent else None,
            "active": consent is not None,
        }
    pending_handoffs = [
        {
            "handoff_id": handoff["handoff_id"],
            "due_at": handoff["due_at"],
            "deadline_anchor_event": handoff["deadline_anchor_event"],
            "pending": [item for item in handoff["items"] if item not in handoff.get("completed_items", [])],
        }
        for handoff in state.handoffs.values()
        if handoff["mentor_id"] == course["mentor_id"]
        and [item for item in handoff["items"] if item not in handoff.get("completed_items", [])]
    ]
    return {
        "artifact_id": artifact_id,
        "lesson": {
            "lesson_id": lesson["lesson_id"],
            "course_id": course["course_id"],
            "delivered_at": lesson.get("delivered_at"),
            "evidence_hash": lesson.get("evidence_hash"),
        },
        "license": {
            "agreement_ref": agreement["agreement_ref"],
            "terms_version": agreement["terms_version"],
            "grant_id": grant["grant_id"],
            "scope": list(grant["scope"]),
            "content_refs": sorted(grant["content_refs"]),
            "expires_at": grant["expires_at"],
            "narrowed_at": grant.get("narrowed_at"),
            "usable_now": not invalid_content,
            "invalid_content_refs": sorted(invalid_content),
        },
        "contributors": artifact.get("contributors", []),
        "attributions": artifact.get("attributions", []),
        "student_consents": consents,
        "publication": artifact.get("publication"),
        "fee_decision": artifact.get("fee_decision"),
        "takedown": artifact.get("takedown"),
        "holds": _holds(state, artifact_id),
        "pending_handoffs": pending_handoffs,
    }


def deadline_register(state: LedgerState, now: datetime) -> dict[str, Any]:
    """期限登记：所有备课、授权、整改、课酬与交接期限及其锚点事件。

    期限值来自事件载荷（绝对时间），重放日志只会还原，不会重新起算。
    """
    items: list[dict[str, Any]] = []
    for course in state.courses.values():
        if not course.get("preparation_confirmed_at"):
            items.append(
                _deadline("preparation", course["preparation_due_at"], course["planned_event_id"], now)
                | {"ref": course["course_id"]}
            )
    for grant in state.grants.values():
        items.append(
            _deadline("grant_expiry", grant["expires_at"], grant["grant_event_id"], now)
            | {"ref": grant["grant_id"], "mentor_id": grant["mentor_id"]}
        )
    for rect in state.rectifications.values():
        if not rect.get("resolved_at"):
            items.append(
                _deadline("rectification", rect["due_at"], rect["deadline_anchor_event"], now)
                | {"ref": rect["rectification_id"], "lesson_id": rect["lesson_id"]}
            )
    for obligation in state.obligations.values():
        if not obligation.get("settled_at"):
            items.append(
                _deadline("payout", obligation["due_at"], obligation["deadline_anchor_event"], now)
                | {"ref": obligation["obligation_id"], "kind": obligation["kind"], "mentor_id": obligation["mentor_id"]}
            )
    for handoff in state.handoffs.values():
        if not handoff.get("completed_at"):
            items.append(
                _deadline("handoff", handoff["due_at"], handoff["deadline_anchor_event"], now)
                | {"ref": handoff["handoff_id"], "mentor_id": handoff["mentor_id"]}
            )
    items.sort(key=lambda item: item["due_at"])
    return {"generated_at": now.isoformat(), "items": items}
