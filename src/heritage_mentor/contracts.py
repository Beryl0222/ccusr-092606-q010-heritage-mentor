"""领域事件交换契约校验。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class ContractIssue:
    field: str
    code: str
    message: str


# 每种事件归属的聚合类型；不属于这张表的配对不得写入日志。
EVENT_AGGREGATE_TYPES: dict[str, str] = {
    "AGREEMENT_VERSIONED": "partnership_agreement",
    "MENTOR_REGISTERED": "mentor_profile",
    "MENTOR_AUTHORIZED": "mentor_grant",
    "GRANT_NARROWED": "mentor_grant",
    "COURSE_PLANNED": "course",
    "PREPARATION_CONFIRMED": "course",
    "LESSON_SCHEDULED": "lesson_session",
    "LESSON_RESCHEDULED": "lesson_session",
    "LESSON_CANCELLED": "lesson_session",
    "DELIVERY_CONFIRMED": "lesson_session",
    "ATTENDANCE_RECORDED": "lesson_session",
    "QUALITY_FEEDBACK_GIVEN": "lesson_session",
    "RECTIFICATION_OPENED": "lesson_session",
    "RECTIFICATION_RESOLVED": "lesson_session",
    "CONSENT_GRANTED": "student_consent",
    "CONSENT_WITHDRAWN": "student_consent",
    "ARTIFACT_REGISTERED": "student_artifact",
    "ARTIFACT_PUBLICATION_DECIDED": "student_artifact",
    "ARTIFACT_FEE_DECIDED": "student_artifact",
    "ATTRIBUTION_SUPPLEMENTED": "student_artifact",
    "ARTIFACT_TAKEDOWN_REQUESTED": "student_artifact",
    "ARTIFACT_TAKEDOWN_COMPLETED": "student_artifact",
    "FEE_OBLIGATION_RAISED": "payout",
    "PAYOUT_SETTLED": "payout",
    "HANDOFF_REQUIRED": "handoff",
    "HANDOFF_COMPLETED": "handoff",
    "SETTLEMENT_HOLD_PLACED": "settlement_hold",
    "SETTLEMENT_HOLD_CLEARED": "settlement_hold",
    "OBLIGATION_RAISED": "handoff",
}

# 载荷中表示时刻/期限的字段：无论出现在哪类事件里都必须携带时区。
TEMPORAL_PAYLOAD_FIELDS = frozenset(
    {
        "effective_from",
        "issued_at",
        "expires_at",
        "preparation_due_at",
        "starts_at",
        "ends_at",
        "delivered_at",
        "granted_at",
        "valid_until",
        "withdrawn_at",
        "decided_at",
        "requested_at",
        "completed_at",
        "resolved_at",
        "opened_at",
        "given_at",
        "due_at",
        "settled_at",
    }
)


def _timezone_is_explicit(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_event(payload: Any, schema: Mapping[str, Any]) -> list[ContractIssue]:
    """返回稳定排序的问题列表，不修改输入。"""
    if not isinstance(payload, Mapping):
        return [ContractIssue("$", "object_required", "事件必须是 JSON 对象")]
    issues: list[ContractIssue] = []
    for field in schema.get("required", []):
        if field not in payload:
            issues.append(ContractIssue(str(field), "required", "缺少必填字段"))
    for field in ("event_id", "event_type", "aggregate_type", "aggregate_id", "idempotency_key"):
        if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
            issues.append(ContractIssue(field, "non_empty_string", "字段必须是非空字符串"))
    version = payload.get("version")
    if "version" in payload and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        issues.append(ContractIssue("version", "positive_integer", "版本必须是正整数"))
    occurred_at = payload.get("occurred_at")
    if "occurred_at" in payload and (not isinstance(occurred_at, str) or not _timezone_is_explicit(occurred_at)):
        issues.append(ContractIssue("occurred_at", "timezone_required", "发生时间必须包含时区"))
    properties = schema.get("properties", {})
    for field in ("event_type", "aggregate_type"):
        allowed = properties.get(field, {}).get("enum", [])
        value = payload.get(field)
        if isinstance(value, str) and allowed and value not in allowed:
            issues.append(ContractIssue(field, "unsupported_value", "字段值未在契约中登记"))
    event_type = payload.get("event_type")
    aggregate_type = payload.get("aggregate_type")
    if (
        isinstance(event_type, str)
        and isinstance(aggregate_type, str)
        and event_type in EVENT_AGGREGATE_TYPES
        and EVENT_AGGREGATE_TYPES[event_type] != aggregate_type
    ):
        issues.append(
            ContractIssue(
                "aggregate_type",
                "aggregate_mismatch",
                f"事件 {event_type} 必须归属于聚合 {EVENT_AGGREGATE_TYPES[event_type]}",
            )
        )
    body = payload.get("payload")
    if "payload" in payload and not isinstance(body, Mapping):
        issues.append(ContractIssue("payload", "object_required", "事件载荷必须是 JSON 对象"))
    elif isinstance(event_type, str) and isinstance(body, Mapping):
        for field in schema.get("payload_required_by_event", {}).get(event_type, []):
            if field not in body:
                issues.append(ContractIssue(f"payload.{field}", "required", "事件载荷缺少必填字段"))
        for field, value in body.items():
            if field in TEMPORAL_PAYLOAD_FIELDS and not (
                isinstance(value, str) and _timezone_is_explicit(value)
            ):
                issues.append(
                    ContractIssue(f"payload.{field}", "timezone_required", "期限字段必须包含时区")
                )
    return sorted(issues, key=lambda issue: (issue.field, issue.code))
