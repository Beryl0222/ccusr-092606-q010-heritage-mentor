"""事件归约：把事件流重放为可查询状态。

纯函数式折叠，不做业务决策；所有结论性状态都能由事件重建，
因此服务重启后期限、授权窗口、结算状态一律不重新起算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class ReducerError(ValueError):
    """事件流违反版本或幂等约束。"""


@dataclass
class Receipt:
    kind: str  # attendance / artifact
    event: Mapping[str, Any]


@dataclass
class LessonState:
    lesson_id: str
    plans: list[Mapping[str, Any]] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    delivery: Mapping[str, Any] | None = None
    feedback: list[Mapping[str, Any]] = field(default_factory=list)
    block_events: list[Mapping[str, Any]] = field(default_factory=list)
    free_events: list[Mapping[str, Any]] = field(default_factory=list)
    settlements: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def plan(self) -> Mapping[str, Any] | None:
        return self.plans[-1] if self.plans else None


@dataclass
class State:
    events: list[Mapping[str, Any]] = field(default_factory=list)
    events_by_id: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    versions: dict[tuple[str, str], list[int]] = field(default_factory=dict)

    agreements: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    mentors: dict[str, dict[str, Any]] = field(default_factory=dict)
    grants: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    series: dict[str, dict[str, Any]] = field(default_factory=dict)
    lessons: dict[str, LessonState] = field(default_factory=dict)
    consents: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    obligations: dict[str, dict[str, Any]] = field(default_factory=dict)
    fee_policies: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    correlations: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    blocks: list[Mapping[str, Any]] = field(default_factory=list)

    # ---- 便捷查询 ------------------------------------------------------

    def mentor(self, mentor_id: str) -> Mapping[str, Any] | None:
        return self.mentors.get(mentor_id)

    def lesson(self, lesson_id: str) -> LessonState | None:
        return self.lessons.get(lesson_id)

    def grants_for(self, mentor_id: str, content_ref: str) -> list[Mapping[str, Any]]:
        result = []
        for history in self.grants.values():
            latest = history[-1]
            body = latest["payload"]
            if body["mentor_id"] == mentor_id and body["content_ref"] == content_ref:
                result.append(latest)
        return result

    def consents_for_student(self, student_id: str) -> list[Mapping[str, Any]]:
        return self.consents.get(student_id, [])

    def open_obligations(self, context_ref: str | None = None) -> list[Mapping[str, Any]]:
        result = []
        for ob in self.obligations.values():
            if ob.get("closed") is not None:
                continue
            if context_ref is not None and ob["raised"]["payload"].get("context_ref") != context_ref:
                continue
            result.append(ob["raised"])
        return result


def _apply_event(state: State, event: Mapping[str, Any]) -> None:
    etype = event["event_type"]
    atype = event["aggregate_type"]
    aid = event["aggregate_id"]
    body = event["payload"]

    if etype == "AGREEMENT_VERSIONED":
        state.agreements.setdefault(aid, []).append(event)
    elif etype == "MENTOR_QUALIFIED":
        mentor = state.mentors.setdefault(
            aid, {"qualified": [], "exit": None}
        )
        mentor["qualified"].append(event)
    elif etype == "MENTOR_EXITED":
        mentor = state.mentors.setdefault(aid, {"qualified": [], "exit": None})
        mentor["exit"] = event
    elif etype == "CONTENT_GRANTED":
        state.grants.setdefault(aid, []).append(event)
    elif etype == "GRADE_PREREQUISITE_SET":
        state.series.setdefault(aid, {"grades": [], "teachers": [], "fee_policy": None})
        state.series[aid]["grades"] = list(body["grades"])
    elif etype == "TEACHER_ROLE_ASSIGNED":
        state.series.setdefault(body["series_id"], {"grades": [], "teachers": [], "fee_policy": None})
        state.series[body["series_id"]]["teachers"].append(event)
    elif etype == "FEE_POLICY_SET":
        state.fee_policies.setdefault(body["series_id"], []).append(event)
        state.series.setdefault(body["series_id"], {"grades": [], "teachers": [], "fee_policy": None})
        state.series[body["series_id"]]["fee_policy"] = event
    elif etype == "LESSON_PLANNED":
        lesson = state.lessons.setdefault(aid, LessonState(lesson_id=aid))
        lesson.plans.append(event)
    elif etype == "RESOURCE_BLOCKED":
        state.blocks.append(event)
        state.lessons.setdefault(body["lesson_id"], LessonState(lesson_id=body["lesson_id"])).block_events.append(event)
    elif etype == "RESOURCE_FREED":
        state.blocks = [b for b in state.blocks if b["event_id"] != body.get("replaces_block_id")]
        state.lessons.setdefault(body["lesson_id"], LessonState(lesson_id=body["lesson_id"])).free_events.append(event)
    elif etype in ("ATTENDANCE_SENT", "ARTIFACT_RECEIPT_SENT"):
        kind = "attendance" if etype == "ATTENDANCE_SENT" else "artifact"
        state.lessons.setdefault(body["lesson_id"], LessonState(lesson_id=body["lesson_id"])).receipts.append(
            Receipt(kind, event)
        )
    elif etype == "DELIVERY_CONFIRMED":
        state.lessons.setdefault(body["lesson_id"], LessonState(lesson_id=body["lesson_id"])).delivery = event
    elif etype == "QUALITY_FEEDBACK_GIVEN":
        state.lessons.setdefault(body["lesson_id"], LessonState(lesson_id=body["lesson_id"])).feedback.append(event)
    elif etype == "STUDENT_CONSENT_GRANTED":
        state.consents.setdefault(body["student_id"], []).append(event)
    elif etype == "ARTIFACT_PUBLISHED":
        state.artifacts.setdefault(aid, {"published": None, "delisted": None})
        state.artifacts[aid]["published"] = event
    elif etype == "ARTIFACT_DELISTED":
        state.artifacts.setdefault(body["artifact_id"], {"published": None, "delisted": None})
        state.artifacts[body["artifact_id"]]["delisted"] = event
    elif etype == "APPROVAL_RECORDED":
        state.approvals[aid] = event
    elif etype == "OBLIGATION_RAISED":
        state.obligations.setdefault(aid, {"raised": None, "closed": None})
        state.obligations[aid]["raised"] = event
    elif etype == "OBLIGATION_CLOSED":
        state.obligations.setdefault(body["obligation_id"], {"raised": None, "closed": None})
        state.obligations[body["obligation_id"]]["closed"] = event
    elif etype == "FEE_SETTLED":
        correlation = body.get("correlation_id")
        if correlation:
            state.correlations[correlation] = event
        state.lessons.setdefault(body["lesson_id"], LessonState(lesson_id=body["lesson_id"])).settlements.append(event)
    else:  # pragma: no cover - 契约层已拦截未知事件
        raise ReducerError(f"未知事件类型: {etype} (聚合 {atype})")


def apply(state: State, event: Mapping[str, Any]) -> State:
    """把单个事件折叠进状态；同一 event_id 原样重放视为幂等空操作。"""
    eid = event["event_id"]
    existing = state.events_by_id.get(eid)
    if existing is not None:
        if existing != event:
            raise ReducerError(f"事件标识冲突: {eid}")
        return state
    key = (event["aggregate_type"], event["aggregate_id"])
    seen = state.versions.setdefault(key, [])
    expected = len(seen) + 1
    if event["version"] != expected:
        raise ReducerError(
            f"聚合 {key} 版本必须连续递增: 期望 v{expected}，收到 v{event['version']}"
        )
    seen.append(event["version"])
    state.events.append(event)
    state.events_by_id[eid] = event
    _apply_event(state, event)
    return state


def replay(events: list[Mapping[str, Any]]) -> State:
    state = State()
    for event in events:
        apply(state, event)
    return state
