"""导师履约簿领域内核：命令决策（decide）与事件归约（apply）。

内核是纯函数式的：:func:`decide` 依据当前状态决定要写入的事件，
:func:`apply_event` 把事件归约进状态。所有期限都以带时区的绝对时间
存放在事件里，重放日志只会还原状态，不会重新起算任何期限。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EVENT_AGGREGATE_TYPES

PUBLICATION_ROLE = "school_affairs"
FEE_ROLE = "finance_bursar"

SCOPE_PARTICIPATION = "participation"
SCOPE_PUBLICATION = "publication"


class DomainError(Exception):
    """命令违反领域规则；``code`` 供上层与测试稳定引用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DomainError("timezone_required", f"时间缺少时区：{value}")
    return parsed


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


@dataclass
class LedgerState:
    agreements: dict[str, dict[str, Any]] = field(default_factory=dict)
    mentors: dict[str, dict[str, Any]] = field(default_factory=dict)
    grants: dict[str, dict[str, Any]] = field(default_factory=dict)
    courses: dict[str, dict[str, Any]] = field(default_factory=dict)
    lessons: dict[str, dict[str, Any]] = field(default_factory=dict)
    consents: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    obligations: dict[str, dict[str, Any]] = field(default_factory=dict)
    rectifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    handoffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # ref(lesson/artifact) -> [hold]；hold 含 reason，部分可被后续事件清除。
    holds: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    receipt_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    resource_capacities: dict[str, int] = field(default_factory=dict)
    event_ids: set[str] = field(default_factory=set)
    idempotency_keys: dict[str, str] = field(default_factory=dict)
    aggregate_versions: dict[tuple[str, str], int] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def grant_for_lesson(self, lesson: Mapping[str, Any]) -> dict[str, Any] | None:
        course = self.courses.get(lesson["course_id"])
        if course is None:
            return None
        return self.grants.get(course["grant_id"])

    def active_holds(self, ref: str) -> list[dict[str, Any]]:
        return [hold for hold in self.holds.get(ref, []) if not hold.get("cleared_at")]


# ---------------------------------------------------------------------------
# 事件构造
# ---------------------------------------------------------------------------


def _next_version(state: LedgerState, aggregate_type: str, aggregate_id: str) -> int:
    key = (aggregate_type, aggregate_id)
    return state.aggregate_versions.get(key, 0) + 1


def make_event(
    state: LedgerState,
    *,
    event_id: str,
    event_type: str,
    aggregate_id: str,
    occurred_at: str,
    payload: Mapping[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if event_id in state.event_ids:
        raise DomainError("event_id_duplicated", f"事件标识重复：{event_id}")
    aggregate_type = EVENT_AGGREGATE_TYPES[event_type]
    event: dict[str, Any] = {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": _next_version(state, aggregate_type, aggregate_id),
        "payload": dict(payload),
    }
    if idempotency_key:
        event["idempotency_key"] = idempotency_key
    return event


# --------------------------------------------------------------------------->
# 规则查询
# ---------------------------------------------------------------------------


def _agreement_active(state: LedgerState, ref: str, at: datetime) -> bool:
    agreement = state.agreements.get(ref)
    return agreement is not None and agreement["status"] == "active" and parse_dt(agreement["effective_from"]) <= at


def _grant_covers(
    state: LedgerState, grant: Mapping[str, Any], at: datetime, content_refs: Iterable[str] = ()
) -> set[str]:
    """返回在 ``at`` 时刻未获授权（过期、被收窄或从未包含）的内容引用集合。"""
    used = set(content_refs)
    invalid: set[str] = set()
    narrowed_raw = grant.get("narrowed_at")
    narrowed_at = parse_dt(narrowed_raw) if narrowed_raw else None
    for ref in used:
        if ref not in grant["content_refs"]:
            invalid.add(ref)
        elif ref in grant.get("removed_content_refs", set()) and (
            narrowed_at is None or at >= narrowed_at
        ):
            invalid.add(ref)
    if not (parse_dt(grant["issued_at"]) <= at < parse_dt(grant["expires_at"])):
        invalid |= used
    return invalid


def _consent_covers(state: LedgerState, consent: Mapping[str, Any], scope: str, at: datetime) -> bool:
    if scope not in consent["scope"]:
        return False
    if not (parse_dt(consent["granted_at"]) <= at < parse_dt(consent["valid_until"])):
        return False
    withdrawn_at = consent.get("withdrawn_at")
    return withdrawn_at is None or at < parse_dt(withdrawn_at)


def _student_consent(state: LedgerState, student_ref: str, scope: str, at: datetime) -> dict[str, Any] | None:
    for consent in state.consents.values():
        if consent["student_ref"] == student_ref and _consent_covers(state, consent, scope, at):
            return consent
    return None


def _open_holds_of_reason(state: LedgerState, ref: str, reason: str) -> list[dict[str, Any]]:
    return [
        hold
        for hold in state.active_holds(ref)
        if hold["reason"] == reason
    ]


def _published_artifacts_with_student(
    state: LedgerState, student_ref: str, at: datetime | None = None
) -> list[dict[str, Any]]:
    result = []
    for artifact in state.artifacts.values():
        publication = artifact.get("publication")
        if not (publication and publication.get("decision") == "approved"):
            continue
        if student_ref not in artifact["student_refs"]:
            continue
        takedown = artifact.get("takedown")
        if takedown:
            # 收窄产生的下架义务在 requested_at 才生效；此前成果仍公开但被标记待下架。
            effective = parse_dt(takedown["requested_at"])
            if at is None or at >= effective:
                continue
        result.append(artifact)
    return result


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------


def decide(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    """把一条命令翻译成零到多条待追加事件；不修改状态。"""
    command_type = command.get("type")
    if not isinstance(command_type, str):
        raise DomainError("command_type_required", "命令必须包含 type")
    handler = _HANDLERS.get(command_type)
    if handler is None:
        raise DomainError("unknown_command", f"未登记的命令类型：{command_type}")
    if command.get("idempotency_key"):
        existing = state.idempotency_keys.get(command["idempotency_key"])
        if existing is not None:
            # 同一幂等键重放：返回首次产生的事件，绝不重复计数。
            return [event for event in state.events if event["event_id"] == existing] or []
    return handler(command, state)


def _cmd_version_agreement(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    payload = {
        "agreement_ref": command["agreement_ref"],
        "terms_version": command["terms_version"],
        "effective_from": command["effective_from"],
        "status": command["status"],
    }
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="AGREEMENT_VERSIONED",
            aggregate_id=command["agreement_ref"],
            occurred_at=command["occurred_at"],
            payload=payload,
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_register_mentor(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    mentor_id = command["mentor_id"]
    if mentor_id in state.mentors:
        raise DomainError("mentor_already_registered", f"导师已登记：{mentor_id}")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="MENTOR_REGISTERED",
            aggregate_id=mentor_id,
            occurred_at=command["occurred_at"],
            payload={"mentor_id": mentor_id, "qualification": command["qualification"]},
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_authorize_grant(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    grant_id = command["grant_id"]
    if grant_id in state.grants:
        raise DomainError("grant_already_issued", f"授权已签发：{grant_id}")
    mentor = state.mentors.get(command["mentor_id"])
    if mentor is None:
        raise DomainError("mentor_unknown", "导师资质尚未登记")
    if not _agreement_active(state, command["agreement_ref"], parse_dt(command["issued_at"])):
        raise DomainError("agreement_inactive", "签发授权时合作协议必须有效")
    if parse_dt(command["expires_at"]) <= parse_dt(command["issued_at"]):
        raise DomainError("grant_window_invalid", "授权到期时间必须晚于签发时间")
    payload = {
        "grant_id": grant_id,
        "mentor_id": command["mentor_id"],
        "agreement_ref": command["agreement_ref"],
        "scope": command["scope"],
        "content_refs": list(command["content_refs"]),
        "grade_prerequisites": list(command["grade_prerequisites"]),
        "issued_at": command["issued_at"],
        "expires_at": command["expires_at"],
    }
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="MENTOR_AUTHORIZED",
            aggregate_id=grant_id,
            occurred_at=command["occurred_at"],
            payload=payload,
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_narrow_grant(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    grant_id = command["grant_id"]
    grant = state.grants.get(grant_id)
    if grant is None:
        raise DomainError("grant_unknown", f"授权不存在：{grant_id}")
    removed = [ref for ref in command["removed_content_refs"] if ref in grant["content_refs"]]
    follow_ups = list(command.get("follow_up_obligations", []))
    events = [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="GRANT_NARROWED",
            aggregate_id=grant_id,
            occurred_at=command["occurred_at"],
            payload={
                "grant_id": grant_id,
                "revised_scope": command["revised_scope"],
                "removed_content_refs": removed,
                "effective_from": command["effective_from"],
                "follow_up_obligations": deepcopy(follow_ups),
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]
    # 收窄只面向未来；已公开且使用了被移除内容的成果产生下架义务。
    auto_takedowns: set[str] = set()
    for artifact in state.artifacts.values():
        publication = artifact.get("publication")
        if (
            publication
            and publication.get("decision") == "approved"
            and not artifact.get("takedown")
            and set(artifact["source_content_refs"]) & set(removed)
        ):
            events.append(_takedown_request_event(state, artifact["artifact_id"], "grant_narrowed", command, 0))
            auto_takedowns.add(artifact["artifact_id"])
    seq = count(1)
    for item in follow_ups:
        kind = item.get("type")
        if kind == "takedown":
            artifact = state.artifacts.get(item["artifact_id"])
            if artifact and not artifact.get("takedown") and item["artifact_id"] not in auto_takedowns:
                events.append(_takedown_request_event(state, item["artifact_id"], item.get("reason", "grant_narrowed"), command, next(seq)))
        elif kind == "attribution":
            artifact = state.artifacts.get(item["artifact_id"])
            if artifact and not _open_holds_of_reason(state, artifact["artifact_id"], "attribution_required"):
                events.append(_hold_event(state, item["artifact_id"], "artifact", "attribution_required", command, next(seq),
                                          {"note": item.get("note", "协议收窄后需补充署名")}))
        elif kind == "handoff":
            events.append(
                make_event(
                    state,
                    event_id=f"{command['event_id']}#handoff-{next(seq)}",
                    event_type="HANDOFF_REQUIRED",
                    aggregate_id=item["handoff_id"],
                    occurred_at=command["occurred_at"],
                    payload={
                        "handoff_id": item["handoff_id"],
                        "mentor_id": grant["mentor_id"],
                        "items": list(item["items"]),
                        "due_at": item["due_at"],
                        "deadline_anchor_event": command["event_id"],
                    },
                )
            )
    return events


def _takedown_request_event(state, artifact_id, reason, command, seq) -> dict[str, Any]:
    suffix = f"#{seq}" if seq else "#auto"
    return make_event(
        state,
        event_id=f"{command['event_id']}{suffix}-takedown-{artifact_id}",
        event_type="ARTIFACT_TAKEDOWN_REQUESTED",
        aggregate_id=artifact_id,
        occurred_at=command["occurred_at"],
        payload={
            "artifact_id": artifact_id,
            "reason": reason,
            "requested_at": command["effective_from"] if "effective_from" in command else command["occurred_at"],
        },
    )


def _hold_event(state, ref, ref_kind, reason, command, seq, details) -> dict[str, Any]:
    return make_event(
        state,
        event_id=f"{command['event_id']}#hold-{seq}-{ref}",
        event_type="SETTLEMENT_HOLD_PLACED",
        aggregate_id=f"hold:{ref}:{reason}:{seq}",
        occurred_at=command["occurred_at"],
        payload={
            "ref": ref,
            "ref_kind": ref_kind,
            "reason": reason,
            "detected_at": command["occurred_at"],
            "details": details,
        },
    )


def _cmd_plan_course(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    course_id = command["course_id"]
    if course_id in state.courses:
        raise DomainError("course_already_planned", f"课程已建：{course_id}")
    grant = state.grants.get(command["grant_id"])
    if grant is None:
        raise DomainError("grant_unknown", "课程必须关联有效授权")
    if grant["mentor_id"] != command["mentor_id"]:
        raise DomainError("mentor_grant_mismatch", "课程导师与授权主体不一致")
    at = parse_dt(command["occurred_at"])
    if not _agreement_active(state, grant["agreement_ref"], at):
        raise DomainError("agreement_inactive", "合作协议未生效，不能开课")
    if command["grade_level"] not in grant["grade_prerequisites"]:
        raise DomainError("grade_prerequisite_failed", "年级不在授权的年级前提内")
    if not command.get("collaborating_teacher_ids"):
        raise DomainError("teacher_required", "校内协作教师不得为空（课堂安全与课程目标责任）")
    payload = {
        "course_id": course_id,
        "mentor_id": command["mentor_id"],
        "agreement_ref": grant["agreement_ref"],
        "grant_id": command["grant_id"],
        "grade_level": command["grade_level"],
        "collaborating_teacher_ids": list(command["collaborating_teacher_ids"]),
        "lesson_count": command["lesson_count"],
        "preparation_due_at": command["preparation_due_at"],
    }
    for optional in ("roster", "quorum", "lesson_fee"):
        if optional in command:
            payload[optional] = command[optional]
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="COURSE_PLANNED",
            aggregate_id=course_id,
            occurred_at=command["occurred_at"],
            payload=payload,
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _resource_needs(command: Mapping[str, Any]) -> dict[str, int]:
    needs: dict[str, int] = {}
    for resource in command.get("resource_window", {}).get("resources", []):
        needs[resource["resource_id"]] = int(resource.get("capacity", 1))
    return needs


def _concurrent_users(
    state: LedgerState, resource_id: str, starts_at: datetime, ends_at: datetime, ignore_lesson_id: str | None
) -> int:
    return sum(
        1
        for other in state.lessons.values()
        if other["status"] == "scheduled"
        and other["lesson_id"] != ignore_lesson_id
        and resource_id in other["resource_needs"]
        and _overlaps(starts_at, ends_at, parse_dt(other["starts_at"]), parse_dt(other["ends_at"]))
    )


def _assert_capacity(
    state: LedgerState,
    mentor_id: str,
    starts_at: datetime,
    ends_at: datetime,
    needs: Mapping[str, int],
    ignore_lesson_id: str | None = None,
) -> None:
    if ends_at <= starts_at:
        raise DomainError("time_window_invalid", "结束时间必须晚于开始时间")
    for resource_id, capacity in needs.items():
        if capacity < 1:
            raise DomainError("resource_capacity_invalid", f"设备 {resource_id} 容量必须为正整数")
        known = state.resource_capacities.get(resource_id)
        if known is not None and known != capacity:
            raise DomainError(
                "resource_capacity_conflict",
                f"设备 {resource_id} 容量登记为 {known}，与本次申报 {capacity} 不一致",
            )
    for lesson in state.lessons.values():
        if lesson["status"] != "scheduled" or lesson["lesson_id"] == ignore_lesson_id:
            continue
        if _overlaps(starts_at, ends_at, parse_dt(lesson["starts_at"]), parse_dt(lesson["ends_at"])):
            if lesson["mentor_id"] == mentor_id:
                raise DomainError(
                    "mentor_overbooked",
                    f"导师 {mentor_id} 在该时段已有课次 {lesson['lesson_id']}，不能超额排期",
                )
            for resource_id, capacity in needs.items():
                if resource_id in lesson["resource_needs"]:
                    # 本窗内并发占用（含本次预约）不得超过设备容量。
                    if _concurrent_users(state, resource_id, starts_at, ends_at, ignore_lesson_id) + 1 > capacity:
                        raise DomainError(
                            "resource_overbooked",
                            f"专用设备 {resource_id} 并发排期超过容量 {capacity}",
                        )


def _assert_grant_window(state: LedgerState, course_id: str, starts_at: datetime, ends_at: datetime) -> None:
    course = state.courses[course_id]
    grant = state.grants[course["grant_id"]]
    if not _agreement_active(state, grant["agreement_ref"], starts_at):
        raise DomainError("agreement_inactive", "课时窗口内合作协议已失效")
    if parse_dt(grant["issued_at"]) > starts_at or parse_dt(grant["expires_at"]) < ends_at:
        raise DomainError("grant_window_lapsed", "课时落在授权期限之外，过期内容不得继续使用")
    # 具体内容是否在收窄后仍可用，在授课确认时按 content_refs_used 校验，
    # 这样已完成课次保留原依据，仅阻止未来使用被移除的内容。


def _cmd_confirm_preparation(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    course = state.courses.get(command["course_id"])
    if course is None:
        raise DomainError("course_unknown", "课程不存在")
    if course.get("preparation_confirmed_at"):
        raise DomainError("preparation_already_confirmed", "备课已确认，不得重复确认")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="PREPARATION_CONFIRMED",
            aggregate_id=command["course_id"],
            occurred_at=command["occurred_at"],
            payload={
                "course_id": command["course_id"],
                "confirmed_at": command["confirmed_at"],
                "deadline_anchor_event": course["planned_event_id"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_schedule_lesson(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    lesson_id = command["lesson_id"]
    if lesson_id in state.lessons:
        raise DomainError("lesson_already_scheduled", f"课次已存在：{lesson_id}")
    course = state.courses.get(command["course_id"])
    if course is None:
        raise DomainError("course_unknown", "课次必须关联已建课程")
    starts_at = parse_dt(command["starts_at"])
    ends_at = parse_dt(command["ends_at"])
    needs = _resource_needs(command)
    _assert_capacity(state, command["mentor_id"], starts_at, ends_at, needs)
    _assert_grant_window(state, command["course_id"], starts_at, ends_at)
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="LESSON_SCHEDULED",
            aggregate_id=lesson_id,
            occurred_at=command["occurred_at"],
            payload={
                "lesson_id": lesson_id,
                "course_id": command["course_id"],
                "mentor_id": command["mentor_id"],
                "class_ref": command["class_ref"],
                "starts_at": command["starts_at"],
                "ends_at": command["ends_at"],
                "resource_window": {"resources": [
                    {"resource_id": rid, "capacity": cap} for rid, cap in needs.items()
                ]},
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_reschedule_lesson(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    lesson = state.lessons.get(command["lesson_id"])
    if lesson is None:
        raise DomainError("lesson_unknown", "课次不存在")
    if lesson["status"] != "scheduled":
        raise DomainError("lesson_not_reschedulable", "仅已排定课次可以调课")
    starts_at = parse_dt(command["starts_at"])
    ends_at = parse_dt(command["ends_at"])
    needs = _resource_needs(command)
    _assert_capacity(state, lesson["mentor_id"], starts_at, ends_at, needs, ignore_lesson_id=lesson["lesson_id"])
    _assert_grant_window(state, lesson["course_id"], starts_at, ends_at)
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="LESSON_RESCHEDULED",
            aggregate_id=lesson["lesson_id"],
            occurred_at=command["occurred_at"],
            payload={
                "lesson_id": lesson["lesson_id"],
                "starts_at": command["starts_at"],
                "ends_at": command["ends_at"],
                "resource_window": {"resources": [
                    {"resource_id": rid, "capacity": cap} for rid, cap in needs.items()
                ]},
                "reason": command["reason"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_cancel_lesson(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    lesson = state.lessons.get(command["lesson_id"])
    if lesson is None:
        raise DomainError("lesson_unknown", "课次不存在")
    if lesson["status"] == "delivered":
        raise DomainError("lesson_already_delivered", "已完成课次不能取消（保留原依据）")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="LESSON_CANCELLED",
            aggregate_id=command["lesson_id"],
            occurred_at=command["occurred_at"],
            payload={"lesson_id": command["lesson_id"], "reason": command["reason"]},
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_confirm_delivery(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    lesson = state.lessons.get(command["lesson_id"])
    if lesson is None:
        raise DomainError("lesson_unknown", "课次不存在")
    if lesson["status"] != "scheduled":
        raise DomainError("lesson_not_scheduled", "仅已排定课次可以确认授课")
    course = state.courses[lesson["course_id"]]
    grant = state.grants[course["grant_id"]]
    delivered_at = parse_dt(command["delivered_at"])
    if not _agreement_active(state, grant["agreement_ref"], delivered_at):
        raise DomainError("agreement_inactive", "授课时合作协议已失效")
    invalid = _grant_covers(state, grant, delivered_at, command["content_refs_used"])
    if invalid:
        raise DomainError("content_not_licensed", f"以下工艺内容在授课时未获授权：{sorted(invalid)}")
    snapshot = list(command["contribution_snapshot"])
    if not any(party.get("role") == "mentor" for party in snapshot):
        raise DomainError("contribution_snapshot_incomplete", "贡献快照必须记录导师贡献")
    payload = {
        "lesson_id": command["lesson_id"],
        "contribution_snapshot": deepcopy(snapshot),
        "content_refs_used": list(command["content_refs_used"]),
        "evidence_hash": command["evidence_hash"],
        "delivered_at": command["delivered_at"],
        "grant_basis": {
            "grant_id": grant["grant_id"],
            "scope": deepcopy(grant["scope"]),
            "issued_at": grant["issued_at"],
            "expires_at": grant["expires_at"],
        },
    }
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="DELIVERY_CONFIRMED",
            aggregate_id=command["lesson_id"],
            occurred_at=command["occurred_at"],
            payload=payload,
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _receipt_identity(receipt: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        receipt["receipt_ref"],
        receipt["student_ref"],
        receipt["recorded_at"],
        receipt.get("fingerprint", ""),
    )


def _cmd_record_attendance(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    lesson = state.lessons.get(command["lesson_id"])
    if lesson is None:
        raise DomainError("lesson_unknown", "课次不存在")
    accepted: list[dict[str, Any]] = []
    seen_in_command: set[str] = set()
    events: list[dict[str, Any]] = []
    hold_seq = count(1)
    for receipt in command["receipts"]:
        ref = receipt["receipt_ref"]
        if ref in seen_in_command:
            continue  # 同一批次重复回执只计一次
        seen_in_command.add(ref)
        previous = state.receipt_index.get(ref)
        identity = (receipt["student_ref"], receipt["recorded_at"], receipt.get("fingerprint", ""))
        if previous is not None:
            if previous["identity"] == identity and previous["ref_kind"] == "attendance":
                continue  # 重复发送只计算一次
            # 内容、参与人或时间不一致：暂停结算。
            events.append(
                make_event(
                    state,
                    event_id=f"{command['event_id']}#hold-{next(hold_seq)}",
                    event_type="SETTLEMENT_HOLD_PLACED",
                    aggregate_id=f"hold:{command['lesson_id']}:receipt_mismatch:{ref}",
                    occurred_at=command["occurred_at"],
                    payload={
                        "ref": command["lesson_id"],
                        "ref_kind": "lesson",
                        "reason": "receipt_mismatch",
                        "detected_at": command["occurred_at"],
                        "details": {"receipt_ref": ref, "previous": list(previous["identity"]), "incoming": list(identity)},
                    },
                )
            )
            continue
        accepted.append(dict(receipt))
    if not accepted and not events:
        return []
    payload: dict[str, Any] = {"lesson_id": command["lesson_id"], "receipts": accepted}
    if accepted:
        events.insert(
            0,
            make_event(
                state,
                event_id=command["event_id"],
                event_type="ATTENDANCE_RECORDED",
                aggregate_id=command["lesson_id"],
                occurred_at=command["occurred_at"],
                payload=payload,
                idempotency_key=command.get("idempotency_key"),
            ),
        )
    elif command.get("idempotency_key"):
        events[0]["idempotency_key"] = command["idempotency_key"]
    return events


def _cmd_grant_consent(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    consent_id = command["consent_id"]
    if consent_id in state.consents:
        raise DomainError("consent_already_granted", f"同意已存在：{consent_id}")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="CONSENT_GRANTED",
            aggregate_id=consent_id,
            occurred_at=command["occurred_at"],
            payload={
                "consent_id": consent_id,
                "student_ref": command["student_ref"],
                "guardian_ref": command["guardian_ref"],
                "scope": list(command["scope"]),
                "granted_at": command["granted_at"],
                "valid_until": command["valid_until"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_withdraw_consent(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    consent = state.consents.get(command["consent_id"])
    if consent is None:
        raise DomainError("consent_unknown", "同意记录不存在")
    if consent.get("withdrawn_at"):
        raise DomainError("consent_already_withdrawn", "同意已撤回")
    events = [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="CONSENT_WITHDRAWN",
            aggregate_id=command["consent_id"],
            occurred_at=command["occurred_at"],
            payload={"consent_id": command["consent_id"], "withdrawn_at": command["withdrawn_at"]},
            idempotency_key=command.get("idempotency_key"),
        )
    ]
    # 撤回监护同意 → 已公开成果立即产生下架义务。
    withdrawn_at = parse_dt(command["withdrawn_at"])
    for artifact in _published_artifacts_with_student(state, consent["student_ref"], withdrawn_at):
        events.append(
            make_event(
                state,
                event_id=f"{command['event_id']}#takedown-{artifact['artifact_id']}",
                event_type="ARTIFACT_TAKEDOWN_REQUESTED",
                aggregate_id=artifact["artifact_id"],
                occurred_at=command["occurred_at"],
                payload={
                    "artifact_id": artifact["artifact_id"],
                    "reason": "consent_withdrawn",
                    "requested_at": command["withdrawn_at"],
                },
            )
        )
    return events


def _cmd_register_artifact(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    artifact_id = command["artifact_id"]
    lesson = state.lessons.get(command["lesson_id"])
    if lesson is None or lesson["status"] != "delivered":
        raise DomainError("lesson_not_delivered", "成果必须来自已完成课次")
    if artifact_id in state.artifacts:
        raise DomainError("artifact_already_registered", f"成果已登记：{artifact_id}")
    events: list[dict[str, Any]] = []
    receipt_ref = command.get("receipt_ref")
    if receipt_ref:
        previous = state.receipt_index.get(receipt_ref)
        identity = (
            command["lesson_id"],
            tuple(sorted(command["student_refs"])),
            tuple(sorted(command["source_content_refs"])),
            command.get("produced_at", ""),
        )
        if previous is not None:
            if previous["identity"] == identity and previous["ref_kind"] == "artifact":
                return []  # 成果回执重复发送只计一次
            events.append(
                make_event(
                    state,
                    event_id=f"{command['event_id']}#hold",
                    event_type="SETTLEMENT_HOLD_PLACED",
                    aggregate_id=f"hold:{artifact_id}:receipt_mismatch:{receipt_ref}",
                    occurred_at=command["occurred_at"],
                    payload={
                        "ref": artifact_id,
                        "ref_kind": "artifact",
                        "reason": "receipt_mismatch",
                        "detected_at": command["occurred_at"],
                        "details": {"receipt_ref": receipt_ref, "previous": list(previous["identity"]), "incoming": list(identity)},
                    },
                )
            )
    payload = {
        "artifact_id": artifact_id,
        "lesson_id": command["lesson_id"],
        "student_refs": list(command["student_refs"]),
        "source_content_refs": list(command["source_content_refs"]),
    }
    for optional in ("receipt_ref", "produced_at", "fingerprint"):
        if optional in command:
            payload[optional] = command[optional]
    events.append(
        make_event(
            state,
            event_id=command["event_id"],
            event_type="ARTIFACT_REGISTERED",
            aggregate_id=artifact_id,
            occurred_at=command["occurred_at"],
            payload=payload,
            idempotency_key=command.get("idempotency_key"),
        )
    )
    return events


def _artifact_new_use_blocked(state: LedgerState, artifact: Mapping[str, Any], at: datetime) -> str | None:
    if artifact.get("takedown"):
        return "artifact_taken_down"
    lesson = state.lessons[artifact["lesson_id"]]
    course = state.courses[lesson["course_id"]]
    grant = state.grants[course["grant_id"]]
    if not _agreement_active(state, grant["agreement_ref"], at):
        return "agreement_inactive"
    invalid = _grant_covers(state, grant, at, artifact["source_content_refs"])
    if invalid:
        return "content_not_licensed"
    for student_ref in artifact["student_refs"]:
        if _student_consent(state, student_ref, SCOPE_PUBLICATION, at) is None:
            return "consent_missing"
    return None


def _cmd_decide_publication(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    artifact = state.artifacts.get(command["artifact_id"])
    if artifact is None:
        raise DomainError("artifact_unknown", "成果不存在")
    if artifact.get("publication"):
        raise DomainError("decision_immutable", "公开批准只能作出一次")
    if command.get("approver_role") != PUBLICATION_ROLE:
        raise DomainError("approver_role_invalid", f"公开传播批准必须由 {PUBLICATION_ROLE} 作出")
    if not command.get("approver_id"):
        raise DomainError("approver_required", "批准人不能为空")
    at = parse_dt(command["decided_at"])
    if command.get("decision") == "approved":
        reason = _artifact_new_use_blocked(state, artifact, at)
        if reason:
            raise DomainError(reason, "当前不满足公开传播条件")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="ARTIFACT_PUBLICATION_DECIDED",
            aggregate_id=command["artifact_id"],
            occurred_at=command["occurred_at"],
            payload={
                "artifact_id": command["artifact_id"],
                "decision": command["decision"],
                "approver_role": command["approver_role"],
                "approver_id": command["approver_id"],
                "decided_at": command["decided_at"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_decide_fee(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    artifact = state.artifacts.get(command["artifact_id"])
    if artifact is None:
        raise DomainError("artifact_unknown", "成果不存在")
    if artifact.get("fee_decision"):
        raise DomainError("decision_immutable", "费用批准只能作出一次")
    if command.get("approver_role") != FEE_ROLE:
        raise DomainError("approver_role_invalid", f"费用批准必须由 {FEE_ROLE} 作出")
    publication = artifact.get("publication")
    if not publication or publication.get("decision") != "approved":
        raise DomainError("publication_not_approved", "产生费用的公开成果必须先获得公开传播批准")
    # 两个批准必须相互独立：角色不同且不是同一自然人。
    if command.get("approver_id") == publication.get("approver_id"):
        raise DomainError("approvals_not_independent", "公开传播与费用批准必须由相互独立的批准人作出")
    at = parse_dt(command["decided_at"])
    if command.get("decision") == "approved":
        reason = _artifact_new_use_blocked(state, artifact, at)
        if reason:
            raise DomainError(reason, "当前不满足费用批准条件")
        if state.active_holds(artifact["artifact_id"]):
            raise DomainError("settlement_on_hold", "存在未澄清的结算暂停事项")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="ARTIFACT_FEE_DECIDED",
            aggregate_id=command["artifact_id"],
            occurred_at=command["occurred_at"],
            payload={
                "artifact_id": command["artifact_id"],
                "decision": command["decision"],
                "approver_role": command["approver_role"],
                "approver_id": command["approver_id"],
                "decided_at": command["decided_at"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_supplement_attribution(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    artifact = state.artifacts.get(command["artifact_id"])
    if artifact is None:
        raise DomainError("artifact_unknown", "成果不存在")
    attributions = list(command["attributions"])
    contributors = {party["party_ref"] for party in artifact.get("contributors", [])}
    missing = contributors - {entry["party_ref"] for entry in attributions}
    if missing:
        raise DomainError("attribution_incomplete", f"仍缺少贡献者署名：{sorted(missing)}")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="ATTRIBUTION_SUPPLEMENTED",
            aggregate_id=command["artifact_id"],
            occurred_at=command["occurred_at"],
            payload={"artifact_id": command["artifact_id"], "attributions": deepcopy(attributions)},
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_complete_takedown(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    artifact = state.artifacts.get(command["artifact_id"])
    if artifact is None or not artifact.get("takedown"):
        raise DomainError("takedown_not_requested", "没有待执行的下架要求")
    if artifact["takedown"].get("completed_at"):
        raise DomainError("takedown_already_completed", "下架已完成")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="ARTIFACT_TAKEDOWN_COMPLETED",
            aggregate_id=command["artifact_id"],
            occurred_at=command["occurred_at"],
            payload={"artifact_id": command["artifact_id"], "completed_at": command["completed_at"]},
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _lesson_fee_blockers(state: LedgerState, lesson: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if lesson["status"] != "delivered":
        blockers.append("lesson_not_delivered")
    quorum = state.courses[lesson["course_id"]].get("quorum", 1)
    if len(lesson.get("attendance", [])) < quorum:
        blockers.append("quorum_unmet")
    if lesson.get("quality") != "pass":
        blockers.append("quality_not_passed")
    if any(not rect.get("resolved_at") for rect in state.rectifications.values() if rect["lesson_id"] == lesson["lesson_id"]):
        blockers.append("rectification_open")
    if state.active_holds(lesson["lesson_id"]):
        blockers.append("settlement_on_hold")
    return blockers


def _cmd_raise_lesson_fee(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    lesson = state.lessons.get(command["lesson_id"])
    if lesson is None:
        raise DomainError("lesson_unknown", "课次不存在")
    blockers = _lesson_fee_blockers(state, lesson)
    if blockers:
        raise DomainError("fee_conditions_unmet", f"课酬条件未满足：{blockers}")
    planned_fee = state.courses[lesson["course_id"]].get("lesson_fee")
    if planned_fee is not None and command["amount"] != planned_fee:
        raise DomainError("fee_amount_mismatch", "课酬金额与课程约定不一致")
    if command["obligation_id"] in state.obligations:
        raise DomainError("obligation_already_raised", "费用义务已生成")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="FEE_OBLIGATION_RAISED",
            aggregate_id=command["obligation_id"],
            occurred_at=command["occurred_at"],
            payload={
                "obligation_id": command["obligation_id"],
                "mentor_id": lesson["mentor_id"],
                "kind": "lesson_fee",
                "amount": command["amount"],
                "source_ref": lesson["lesson_id"],
                "due_at": command["due_at"],
                "deadline_anchor_event": lesson["delivery_event_id"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_raise_artifact_fee(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    artifact = state.artifacts.get(command["artifact_id"])
    if artifact is None:
        raise DomainError("artifact_unknown", "成果不存在")
    fee_decision = artifact.get("fee_decision")
    if not fee_decision or fee_decision.get("decision") != "approved":
        raise DomainError("fee_not_approved", "成果额外课酬缺少独立费用批准")
    if artifact.get("takedown"):
        raise DomainError("artifact_taken_down", "已进入下架流程的成果不得新增费用")
    if state.active_holds(artifact["artifact_id"]):
        raise DomainError("settlement_on_hold", "存在未澄清的结算暂停事项")
    contributors = {party["party_ref"] for party in artifact.get("contributors", [])}
    attributed = {entry["party_ref"] for entry in artifact.get("attributions", [])}
    if contributors - attributed:
        raise DomainError("attribution_incomplete", "费用生成前必须完成全部贡献署名")
    if command["obligation_id"] in state.obligations:
        raise DomainError("obligation_already_raised", "费用义务已生成")
    lesson = state.lessons[artifact["lesson_id"]]
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="FEE_OBLIGATION_RAISED",
            aggregate_id=command["obligation_id"],
            occurred_at=command["occurred_at"],
            payload={
                "obligation_id": command["obligation_id"],
                "mentor_id": lesson["mentor_id"],
                "kind": "artifact_fee",
                "amount": command["amount"],
                "source_ref": artifact["artifact_id"],
                "due_at": command["due_at"],
                "deadline_anchor_event": fee_decision["event_id"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_settle_payout(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    obligation = state.obligations.get(command["obligation_id"])
    if obligation is None:
        raise DomainError("obligation_unknown", "费用义务不存在")
    if obligation.get("settled_at"):
        raise DomainError("obligation_already_settled", "费用已结清，不得重复支付")
    if command["amount"] != obligation["amount"]:
        raise DomainError("fee_amount_mismatch", "支付金额与应付金额不一致")
    source_ref = obligation["source_ref"]
    if state.active_holds(source_ref):
        raise DomainError("settlement_on_hold", "回执不一致等暂停事项未澄清，暂停结算")
    if obligation["kind"] == "lesson_fee":
        lesson = state.lessons[source_ref]
        blockers = [b for b in _lesson_fee_blockers(state, lesson) if b != "lesson_not_delivered"]
        if blockers:
            raise DomainError("fee_conditions_unmet", f"结算时条件不再满足：{blockers}")
    else:
        artifact = state.artifacts.get(source_ref)
        if artifact is not None and artifact.get("takedown") and not artifact["takedown"].get("completed_at"):
            raise DomainError("takedown_pending", "下架义务尚未履行完成，暂停该成果费用结算")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="PAYOUT_SETTLED",
            aggregate_id=command["obligation_id"],
            occurred_at=command["occurred_at"],
            payload={
                "obligation_id": command["obligation_id"],
                "settled_at": command["settled_at"],
                "amount": command["amount"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_give_feedback(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    lesson = state.lessons.get(command["lesson_id"])
    if lesson is None or lesson["status"] != "delivered":
        raise DomainError("lesson_not_delivered", "只能对已完成课次反馈质量")
    events = [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="QUALITY_FEEDBACK_GIVEN",
            aggregate_id=command["lesson_id"],
            occurred_at=command["occurred_at"],
            payload={
                "lesson_id": command["lesson_id"],
                "verdict": command["verdict"],
                "given_at": command["given_at"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]
    if command["verdict"] in ("conditional", "fail") and command.get("rectification"):
        rect = command["rectification"]
        events.append(
            make_event(
                state,
                event_id=f"{command['event_id']}#rectification",
                event_type="RECTIFICATION_OPENED",
                aggregate_id=rect["rectification_id"],
                occurred_at=command["occurred_at"],
                payload={
                    "rectification_id": rect["rectification_id"],
                    "lesson_id": command["lesson_id"],
                    "opened_at": command["given_at"],
                    "due_at": rect["due_at"],
                    "deadline_anchor_event": command["event_id"],
                },
            )
        )
    return events


def _cmd_resolve_rectification(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    rectification = state.rectifications.get(command["rectification_id"])
    if rectification is None:
        raise DomainError("rectification_unknown", "整改记录不存在")
    if rectification.get("resolved_at"):
        raise DomainError("rectification_already_resolved", "整改已闭环")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="RECTIFICATION_RESOLVED",
            aggregate_id=command["rectification_id"],
            occurred_at=command["occurred_at"],
            payload={"rectification_id": command["rectification_id"], "resolved_at": command["resolved_at"]},
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_complete_handoff(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    handoff = state.handoffs.get(command["handoff_id"])
    if handoff is None:
        raise DomainError("handoff_unknown", "交接要求不存在")
    if handoff.get("completed_at"):
        raise DomainError("handoff_already_completed", "交接已完成")
    pending = set(handoff["items"]) - set(command["items"])
    if pending:
        raise DomainError("handoff_items_missing", f"交接项未完成：{sorted(pending)}")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="HANDOFF_COMPLETED",
            aggregate_id=command["handoff_id"],
            occurred_at=command["occurred_at"],
            payload={
                "handoff_id": command["handoff_id"],
                "completed_at": command["completed_at"],
                "items": list(command["items"]),
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


def _cmd_resolve_hold(command: Mapping[str, Any], state: LedgerState) -> list[dict[str, Any]]:
    hold_id = command["hold_id"]
    target = None
    for ref, holds in state.holds.items():
        for hold in holds:
            if hold["hold_id"] == hold_id:
                target = (ref, hold)
                break
    if target is None:
        raise DomainError("hold_unknown", "暂停事项不存在")
    _, hold = target
    if hold.get("cleared_at"):
        raise DomainError("hold_already_cleared", "暂停事项已闭环")
    return [
        make_event(
            state,
            event_id=command["event_id"],
            event_type="SETTLEMENT_HOLD_CLEARED",
            aggregate_id=hold_id,
            occurred_at=command["occurred_at"],
            payload={
                "hold_id": hold_id,
                "resolution": command["resolution"],
                "cleared_at": command["cleared_at"],
            },
            idempotency_key=command.get("idempotency_key"),
        )
    ]


_HANDLERS = {
    "version_agreement": _cmd_version_agreement,
    "register_mentor": _cmd_register_mentor,
    "authorize_grant": _cmd_authorize_grant,
    "narrow_grant": _cmd_narrow_grant,
    "plan_course": _cmd_plan_course,
    "confirm_preparation": _cmd_confirm_preparation,
    "schedule_lesson": _cmd_schedule_lesson,
    "reschedule_lesson": _cmd_reschedule_lesson,
    "cancel_lesson": _cmd_cancel_lesson,
    "confirm_delivery": _cmd_confirm_delivery,
    "record_attendance": _cmd_record_attendance,
    "grant_consent": _cmd_grant_consent,
    "withdraw_consent": _cmd_withdraw_consent,
    "register_artifact": _cmd_register_artifact,
    "decide_publication": _cmd_decide_publication,
    "decide_fee": _cmd_decide_fee,
    "supplement_attribution": _cmd_supplement_attribution,
    "complete_takedown": _cmd_complete_takedown,
    "raise_lesson_fee": _cmd_raise_lesson_fee,
    "raise_artifact_fee": _cmd_raise_artifact_fee,
    "settle_payout": _cmd_settle_payout,
    "give_feedback": _cmd_give_feedback,
    "resolve_rectification": _cmd_resolve_rectification,
    "complete_handoff": _cmd_complete_handoff,
    "resolve_hold": _cmd_resolve_hold,
}


# ---------------------------------------------------------------------------
# 归约
# ---------------------------------------------------------------------------


def apply_event(event: Mapping[str, Any], state: LedgerState) -> None:
    """把一条已校验事件归约进状态（原地修改）。"""
    event_id = event["event_id"]
    if event_id in state.event_ids:
        raise DomainError("event_id_duplicated", f"事件标识重复：{event_id}")
    key = event.get("idempotency_key")
    if key and key in state.idempotency_keys:
        raise DomainError("idempotency_key_conflict", f"幂等键冲突：{key}")
    state.event_ids.add(event_id)
    if key:
        state.idempotency_keys[key] = event_id
    state.aggregate_versions[(event["aggregate_type"], event["aggregate_id"])] = event["version"]
    body = event["payload"]
    handler = _APPLIERS.get(event["event_type"])
    if handler:
        handler(event, body, state)
    state.events.append(dict(event))


def _apply_agreement(event, body, state: LedgerState) -> None:
    state.agreements[body["agreement_ref"]] = {
        "agreement_ref": body["agreement_ref"],
        "terms_version": body["terms_version"],
        "effective_from": body["effective_from"],
        "status": body["status"],
        "event_id": event_id_of(event),
    }


def event_id_of(event: Mapping[str, Any]) -> str:
    return event["event_id"]


def _apply_mentor(event, body, state: LedgerState) -> None:
    state.mentors[body["mentor_id"]] = {"mentor_id": body["mentor_id"], "qualification": body["qualification"]}


def _apply_grant(event, body, state: LedgerState) -> None:
    state.grants[body["grant_id"]] = {
        "grant_id": body["grant_id"],
        "mentor_id": body["mentor_id"],
        "agreement_ref": body["agreement_ref"],
        "scope": body["scope"],
        "content_refs": set(body["content_refs"]),
        "grade_prerequisites": set(body["grade_prerequisites"]),
        "issued_at": body["issued_at"],
        "expires_at": body["expires_at"],
        "grant_event_id": event_id_of(event),
    }


def _apply_narrowing(event, body, state: LedgerState) -> None:
    grant = state.grants[body["grant_id"]]
    grant["removed_content_refs"] = set(body["removed_content_refs"]) | grant.get("removed_content_refs", set())
    grant["scope"] = body["revised_scope"]
    grant["narrowed_at"] = body["effective_from"]
    grant["narrowing_event_id"] = event["event_id"]
    grant["follow_up_obligations"] = body["follow_up_obligations"]
    grant["content_refs_after_narrowing"] = grant["content_refs"] - grant["removed_content_refs"]


def _apply_course(event, body, state: LedgerState) -> None:
    state.courses[body["course_id"]] = {
        "course_id": body["course_id"],
        "mentor_id": body["mentor_id"],
        "agreement_ref": body["agreement_ref"],
        "grant_id": body["grant_id"],
        "grade_level": body["grade_level"],
        "collaborating_teacher_ids": list(body["collaborating_teacher_ids"]),
        "lesson_count": body["lesson_count"],
        "preparation_due_at": body["preparation_due_at"],
        "roster": list(body.get("roster", [])),
        "quorum": body.get("quorum", 1),
        "lesson_fee": body.get("lesson_fee"),
        "planned_event_id": event_id_of(event),
        "lessons": [],
    }


def _apply_preparation_confirmed(event, body, state: LedgerState) -> None:
    course = state.courses[body["course_id"]]
    course["preparation_confirmed_at"] = body["confirmed_at"]
    course["preparation_event_id"] = event["event_id"]


def _apply_schedule(event, body, state: LedgerState) -> None:
    resources = body.get("resource_window", {}).get("resources", [])
    needs = {r["resource_id"]: int(r.get("capacity", 1)) for r in resources}
    for resource_id, capacity in needs.items():
        state.resource_capacities[resource_id] = capacity
    state.lessons[body["lesson_id"]] = {
        "lesson_id": body["lesson_id"],
        "course_id": body["course_id"],
        "mentor_id": body["mentor_id"],
        "class_ref": body["class_ref"],
        "starts_at": body["starts_at"],
        "ends_at": body["ends_at"],
        "resource_needs": needs,
        "status": "scheduled",
        "attendance": [],
    }
    state.courses[body["course_id"]]["lessons"].append(body["lesson_id"])


def _apply_reschedule(event, body, state: LedgerState) -> None:
    lesson = state.lessons[body["lesson_id"]]
    lesson["starts_at"] = body["starts_at"]
    lesson["ends_at"] = body["ends_at"]
    resources = body.get("resource_window", {}).get("resources", [])
    needs = {r["resource_id"]: int(r.get("capacity", 1)) for r in resources}
    for resource_id, capacity in needs.items():
        state.resource_capacities[resource_id] = capacity
    lesson["resource_needs"] = needs


def _apply_cancel(event, body, state: LedgerState) -> None:
    state.lessons[body["lesson_id"]]["status"] = "cancelled"


def _apply_delivery(event, body, state: LedgerState) -> None:
    lesson = state.lessons[body["lesson_id"]]
    lesson["status"] = "delivered"
    lesson["contribution_snapshot"] = body["contribution_snapshot"]
    lesson["content_refs_used"] = list(body["content_refs_used"])
    lesson["evidence_hash"] = body["evidence_hash"]
    lesson["delivered_at"] = body["delivered_at"]
    lesson["grant_basis"] = body["grant_basis"]
    lesson["delivery_event_id"] = event["event_id"]


def _apply_attendance(event, body, state: LedgerState) -> None:
    lesson = state.lessons[body["lesson_id"]]
    for receipt in body["receipts"]:
        ref = receipt["receipt_ref"]
        if ref in state.receipt_index:
            continue
        lesson["attendance"].append(receipt)
        state.receipt_index[ref] = {
            "ref_kind": "attendance",
            "owner_ref": body["lesson_id"],
            "identity": (receipt["student_ref"], receipt["recorded_at"], receipt.get("fingerprint", "")),
        }


def _apply_consent(event, body, state: LedgerState) -> None:
    state.consents[body["consent_id"]] = {
        "consent_id": body["consent_id"],
        "student_ref": body["student_ref"],
        "guardian_ref": body["guardian_ref"],
        "scope": list(body["scope"]),
        "granted_at": body["granted_at"],
        "valid_until": body["valid_until"],
    }


def _apply_consent_withdrawn(event, body, state: LedgerState) -> None:
    state.consents[body["consent_id"]]["withdrawn_at"] = body["withdrawn_at"]


def _apply_artifact(event, body, state: LedgerState) -> None:
    lesson = state.lessons[body["lesson_id"]]
    contributors: dict[str, dict[str, Any]] = {}
    for party in lesson.get("contribution_snapshot", []):
        contributors[party["party_ref"]] = {
            "party_ref": party["party_ref"],
            "role": party.get("role"),
            "share": party.get("share"),
        }
    for student_ref in body["student_refs"]:
        contributors.setdefault(student_ref, {"party_ref": student_ref, "role": "student", "share": None})
    artifact = {
        "artifact_id": body["artifact_id"],
        "lesson_id": body["lesson_id"],
        "student_refs": list(body["student_refs"]),
        "source_content_refs": list(body["source_content_refs"]),
        "contributors": list(contributors.values()),
        "attributions": [],
        "produced_at": body.get("produced_at"),
    }
    state.artifacts[body["artifact_id"]] = artifact
    ref = body.get("receipt_ref")
    if ref:
        state.receipt_index[ref] = {
            "ref_kind": "artifact",
            "owner_ref": body["artifact_id"],
            "identity": (
                body["lesson_id"],
                tuple(sorted(body["student_refs"])),
                tuple(sorted(body["source_content_refs"])),
                body.get("produced_at", ""),
            ),
        }


def _apply_publication(event, body, state: LedgerState) -> None:
    state.artifacts[body["artifact_id"]]["publication"] = {
        "decision": body["decision"],
        "approver_role": body["approver_role"],
        "approver_id": body["approver_id"],
        "decided_at": body["decided_at"],
        "event_id": event["event_id"],
    }


def _apply_fee_decision(event, body, state: LedgerState) -> None:
    state.artifacts[body["artifact_id"]]["fee_decision"] = {
        "decision": body["decision"],
        "approver_role": body["approver_role"],
        "approver_id": body["approver_id"],
        "decided_at": body["decided_at"],
        "event_id": event["event_id"],
    }


def _apply_attribution(event, body, state: LedgerState) -> None:
    artifact = state.artifacts[body["artifact_id"]]
    artifact["attributions"] = body["attributions"]
    for hold in state.holds.get(body["artifact_id"], []):
        if hold["reason"] == "attribution_required" and not hold.get("cleared_at"):
            hold["cleared_at"] = event["occurred_at"]


def _apply_takedown_request(event, body, state: LedgerState) -> None:
    artifact = state.artifacts[body["artifact_id"]]
    existing = artifact.get("takedown")
    if existing and not existing.get("completed_at"):
        return
    artifact["takedown"] = {"requested_at": body["requested_at"], "reason": body["reason"], "request_event_id": event["event_id"]}


def _apply_takedown_completed(event, body, state: LedgerState) -> None:
    takedown = state.artifacts[body["artifact_id"]]["takedown"]
    takedown["completed_at"] = body["completed_at"]
    takedown["complete_event_id"] = event["event_id"]


def _apply_obligation(event, body, state: LedgerState) -> None:
    state.obligations[body["obligation_id"]] = {
        "obligation_id": body["obligation_id"],
        "mentor_id": body["mentor_id"],
        "kind": body["kind"],
        "amount": body["amount"],
        "source_ref": body["source_ref"],
        "due_at": body["due_at"],
        "deadline_anchor_event": body["deadline_anchor_event"],
        "raised_event_id": event["event_id"],
    }


def _apply_payout(event, body, state: LedgerState) -> None:
    obligation = state.obligations[body["obligation_id"]]
    obligation["settled_at"] = body["settled_at"]
    obligation["amount"] = body["amount"]
    obligation["payout_event_id"] = event["event_id"]


def _apply_feedback(event, body, state: LedgerState) -> None:
    state.lessons[body["lesson_id"]]["quality"] = body["verdict"]


def _apply_rectification(event, body, state: LedgerState) -> None:
    state.rectifications[body["rectification_id"]] = {
        "rectification_id": body["rectification_id"],
        "lesson_id": body["lesson_id"],
        "opened_at": body["opened_at"],
        "due_at": body["due_at"],
        "deadline_anchor_event": body["deadline_anchor_event"],
    }


def _apply_rectification_resolved(event, body, state: LedgerState) -> None:
    state.rectifications[body["rectification_id"]]["resolved_at"] = body["resolved_at"]


def _apply_handoff(event, body, state: LedgerState) -> None:
    state.handoffs[body["handoff_id"]] = {
        "handoff_id": body["handoff_id"],
        "mentor_id": body["mentor_id"],
        "items": list(body["items"]),
        "due_at": body["due_at"],
        "deadline_anchor_event": body["deadline_anchor_event"],
    }


def _apply_handoff_completed(event, body, state: LedgerState) -> None:
    handoff = state.handoffs[body["handoff_id"]]
    handoff["completed_at"] = body["completed_at"]
    handoff["completed_items"] = list(body["items"])


def _apply_hold(event, body, state: LedgerState) -> None:
    hold = {
        "hold_id": event["aggregate_id"],
        "reason": body["reason"],
        "detected_at": body["detected_at"],
        "details": body.get("details", {}),
        "cleared_at": None,
    }
    state.holds.setdefault(body["ref"], []).append(hold)


def _apply_hold_cleared(event, body, state: LedgerState) -> None:
    for holds in state.holds.values():
        for hold in holds:
            if hold["hold_id"] == body["hold_id"]:
                hold["cleared_at"] = body["cleared_at"]
                hold["resolution"] = body["resolution"]
                return
    raise DomainError("hold_unknown", f"暂停事项不存在：{body['hold_id']}")


_APPLIERS = {
    "AGREEMENT_VERSIONED": _apply_agreement,
    "MENTOR_REGISTERED": _apply_mentor,
    "MENTOR_AUTHORIZED": _apply_grant,
    "GRANT_NARROWED": _apply_narrowing,
    "COURSE_PLANNED": _apply_course,
    "PREPARATION_CONFIRMED": _apply_preparation_confirmed,
    "LESSON_SCHEDULED": _apply_schedule,
    "LESSON_RESCHEDULED": _apply_reschedule,
    "LESSON_CANCELLED": _apply_cancel,
    "DELIVERY_CONFIRMED": _apply_delivery,
    "ATTENDANCE_RECORDED": _apply_attendance,
    "CONSENT_GRANTED": _apply_consent,
    "CONSENT_WITHDRAWN": _apply_consent_withdrawn,
    "ARTIFACT_REGISTERED": _apply_artifact,
    "ARTIFACT_PUBLICATION_DECIDED": _apply_publication,
    "ARTIFACT_FEE_DECIDED": _apply_fee_decision,
    "ATTRIBUTION_SUPPLEMENTED": _apply_attribution,
    "ARTIFACT_TAKEDOWN_REQUESTED": _apply_takedown_request,
    "ARTIFACT_TAKEDOWN_COMPLETED": _apply_takedown_completed,
    "FEE_OBLIGATION_RAISED": _apply_obligation,
    "PAYOUT_SETTLED": _apply_payout,
    "QUALITY_FEEDBACK_GIVEN": _apply_feedback,
    "RECTIFICATION_OPENED": _apply_rectification,
    "RECTIFICATION_RESOLVED": _apply_rectification_resolved,
    "HANDOFF_REQUIRED": _apply_handoff,
    "HANDOFF_COMPLETED": _apply_handoff_completed,
    "SETTLEMENT_HOLD_PLACED": _apply_hold,
    "SETTLEMENT_HOLD_CLEARED": _apply_hold_cleared,
}


def replay(events: Iterable[Mapping[str, Any]]) -> LedgerState:
    state = LedgerState()
    for event in events:
        apply_event(event, state)
    return state


def decide_and_apply(command: Mapping[str, Any], state: LedgerState) -> Sequence[dict[str, Any]]:
    """便捷入口：决策成功后把事件归约进状态并返回它们。

    命中幂等键时返回首次产生的事件，但不会再次归约——重复发送只计一次。
    """
    events = decide(command, state)
    for event in events:
        if event["event_id"] not in state.event_ids:
            apply_event(event, state)
    return events
