"""履约簿领域决策服务。

职责边界：
- 契约层（contracts）保证事件信封可交换；
- 本来（state/store）保证事件可重放、重启不丢状态；
- 本层把题面业务规则落成"决策 + 事件"：开课条件、超额排期、
  送达比对、独立批准、收窄不溯及、义务与费用。

所有期限均锚定事件 occurred_at，由策略天数推导，重启不重新起算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from . import timeutil
from .state import State
from .store import EventStore

SCOPE_CLASS = "in_class"

KIND_RECTIFY = "rectify"
KIND_DELIST_OR_ATTRIBUTE = "delist_or_attribute"
KIND_HANDOVER = "handover"

APPROVAL_MENTOR = "mentor"
APPROVAL_SCHOOL = "school"


@dataclass(frozen=True)
class Policy:
    prep_days: int = 3
    rectify_days: int = 7
    handover_days: int = 14
    payment_days: int = 15


@dataclass
class Decision:
    ok: bool
    code: str
    issues: list[str] = field(default_factory=list)
    events: list[Mapping[str, Any]] = field(default_factory=list)
    idempotent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "issues": self.issues,
            "idempotent": self.idempotent,
            "event_ids": [e["event_id"] for e in self.events],
        }


class ServiceError(ValueError):
    pass


class Ledger:
    def __init__(self, store: EventStore, policy: Policy | None = None):
        self.store = store
        self.policy = policy or Policy()

    # ---- 内部工具 ------------------------------------------------------

    @property
    def s(self) -> State:
        return self.store.state

    def _next_version(self, agg_type: str, agg_id: str) -> int:
        return len(self.s.versions.setdefault((agg_type, agg_id), [])) + 1

    def _append(
        self,
        etype: str,
        agg_type: str,
        agg_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
    ) -> dict[str, Any]:
        version = self._next_version(agg_type, agg_id)
        event = {
            "event_id": event_id or f"{etype.lower()}:{agg_id}:v{version}",
            "event_type": etype,
            "aggregate_type": agg_type,
            "aggregate_id": agg_id,
            "occurred_at": occurred_at,
            "version": version,
            "payload": dict(payload),
        }
        self.store.append(event)
        return event

    def _latest_agreement(self, agreement_id: str) -> Mapping[str, Any] | None:
        history = self.s.agreements.get(agreement_id)
        return history[-1] if history else None

    def _active_grant(self, mentor_id: str, content_ref: str, at: str) -> Mapping[str, Any] | None:
        """返回该时点有效的最新授权；过期或未生效均视为无授权。"""
        moment = timeutil.parse(at)
        candidates = [
            g
            for g in self.s.grants_for(mentor_id, content_ref)
            if timeutil.window_covers(
                g["payload"].get("valid_from"), g["payload"].get("valid_to"), moment
            )
        ]
        return candidates[-1] if candidates else None

    def _active_teacher_roles(self, series_id: str, at: str) -> set[str]:
        moment = timeutil.parse(at)
        series = self.s.series.get(series_id)
        if not series:
            return set()
        roles: set[str] = set()
        for ev in series["teachers"]:
            body = ev["payload"]
            if timeutil.window_covers(body.get("active_from"), body.get("active_to"), moment):
                roles.add(body["role"])
        return roles

    def _agreement_is_narrowed(self, mentor_id: str, at: str) -> bool:
        """导师在 at 时点适用的协议是否已被收窄（收窄事件不早于 at 即阻断未来用途）。"""
        mentor = self.s.mentors.get(mentor_id)
        if not mentor or not mentor["qualified"]:
            return False
        agreement_id = mentor["qualified"][-1]["payload"]["agreement_id"]
        agreement = self._latest_agreement(agreement_id)
        if not agreement or not agreement["payload"].get("narrowed"):
            return False
        return timeutil.parse(agreement["occurred_at"]) <= timeutil.parse(at)

    # ---- 协议 / 导师 / 授权 / 课程基础 ---------------------------------

    def record_agreement(
        self, agreement_id: str, change_summary: str, narrowed: bool, occurred_at: str,
        event_id: str | None = None,
    ) -> Decision:
        event = self._append(
            "AGREEMENT_VERSIONED", "partnership_agreement", agreement_id, occurred_at,
            {"change_summary": change_summary, "narrowed": narrowed}, event_id,
        )
        extra: list[Mapping[str, Any]] = []
        if narrowed:
            extra = self._raise_narrowing_obligations(agreement_id, occurred_at)
        return Decision(True, "agreement_versioned", events=[event, *extra])

    def qualify_mentor(
        self, mentor_id: str, agreement_id: str, discipline: str,
        valid_from: str, valid_to: str | None, content_refs: list[str],
        occurred_at: str, event_id: str | None = None,
    ) -> Decision:
        if self._latest_agreement(agreement_id) is None:
            return Decision(False, "agreement_missing", ["合作协议不存在，不能登记导师资质"])
        event = self._append(
            "MENTOR_QUALIFIED", "mentor_profile", mentor_id, occurred_at,
            {
                "mentor_id": mentor_id, "agreement_id": agreement_id,
                "discipline": discipline, "valid_from": valid_from,
                "valid_to": valid_to, "content_refs": list(content_refs),
            },
            event_id,
        )
        return Decision(True, "mentor_qualified", events=[event])

    def grant_content(
        self, grant_id: str, mentor_id: str, agreement_id: str, content_ref: str,
        scope: list[str], valid_from: str, valid_to: str | None,
        occurred_at: str, event_id: str | None = None,
    ) -> Decision:
        if self._latest_agreement(agreement_id) is None:
            return Decision(False, "agreement_missing", ["合作协议不存在，不能登记内容授权"])
        event = self._append(
            "CONTENT_GRANTED", "content_grant", grant_id, occurred_at,
            {
                "mentor_id": mentor_id, "agreement_id": agreement_id,
                "content_ref": content_ref, "scope": list(scope),
                "valid_from": valid_from, "valid_to": valid_to,
            },
            event_id,
        )
        return Decision(True, "content_granted", events=[event])

    def set_grades(self, series_id: str, grades: list[str], occurred_at: str) -> Decision:
        event = self._append(
            "GRADE_PREREQUISITE_SET", "lesson_series", series_id, occurred_at,
            {"series_id": series_id, "grades": list(grades)},
        )
        return Decision(True, "grades_set", events=[event])

    def assign_teacher(
        self, series_id: str, teacher_id: str, role: str,
        active_from: str, active_to: str | None, occurred_at: str,
    ) -> Decision:
        if role not in ("safety", "curriculum"):
            return Decision(False, "unsupported_role", [f"教师协作角色未登记: {role}"])
        event = self._append(
            "TEACHER_ROLE_ASSIGNED", "lesson_series", series_id, occurred_at,
            {
                "series_id": series_id, "teacher_id": teacher_id, "role": role,
                "active_from": active_from, "active_to": active_to,
            },
        )
        return Decision(True, "teacher_assigned", events=[event])

    def set_fee_policy(
        self, series_id: str, base_fee_per_session: float, artifact_royalty: float,
        occurred_at: str,
    ) -> Decision:
        event = self._append(
            "FEE_POLICY_SET", "fee_account", series_id, occurred_at,
            {
                "series_id": series_id,
                "base_fee_per_session": base_fee_per_session,
                "artifact_royalty": artifact_royalty,
            },
        )
        return Decision(True, "fee_policy_set", events=[event])

    # ---- 排期与开课条件 ------------------------------------------------

    def readiness(
        self, plan: Mapping[str, Any], *, at: str | None = None,
        exclude_lesson: str | None = None, planned_at: str | None = None,
    ) -> list[str]:
        """返回开课阻断项代码列表；为空即具备开课条件。"""
        at = at or plan["scheduled_at"]
        blockers: list[str] = []
        mentor = self.s.mentors.get(plan["mentor_id"])
        if mentor is None:
            blockers.append("mentor_qualification_missing")
        else:
            if mentor.get("exit") is not None:
                blockers.append("mentor_exited")
            else:
                latest = mentor["qualified"][-1]["payload"]
                if not timeutil.window_covers(latest.get("valid_from"), latest.get("valid_to"), timeutil.parse(at)):
                    blockers.append("mentor_qualification_expired")
                agreement = self._latest_agreement(latest["agreement_id"])
                if agreement is None:
                    blockers.append("agreement_missing")
                elif agreement["payload"].get("narrowed") and timeutil.parse(agreement["occurred_at"]) <= timeutil.parse(at):
                    blockers.append("agreement_narrowed")
        for content_ref in plan["required_grants"]:
            grant = self._active_grant(plan["mentor_id"], content_ref, at)
            if grant is None:
                if any(
                    timeutil.parse(g["payload"]["valid_from"]) <= timeutil.parse(at)
                    for g in self.s.grants_for(plan["mentor_id"], content_ref)
                ):
                    blockers.append(f"grant_expired:{content_ref}")
                else:
                    blockers.append(f"grant_missing:{content_ref}")
            elif SCOPE_CLASS not in grant["payload"]["scope"]:
                blockers.append(f"grant_scope_insufficient:{content_ref}")
        series = self.s.series.get(plan["series_id"])
        if series is None or not series["grades"]:
            blockers.append("grade_prerequisite_missing")
        elif plan["grade"] not in series["grades"]:
            blockers.append("grade_not_allowed")
        active_roles = self._active_teacher_roles(plan["series_id"], at)
        if "safety" not in active_roles:
            blockers.append("safety_teacher_not_assigned")
        if "curriculum" not in active_roles:
            blockers.append("curriculum_teacher_not_assigned")
        blockers.extend(self._schedule_conflicts(plan, exclude_lesson=exclude_lesson))
        return self._dedup(blockers)

    def _schedule_conflicts(self, plan: Mapping[str, Any], *, exclude_lesson: str | None = None) -> list[str]:
        start, end = timeutil.parse(plan["scheduled_at"]), timeutil.parse(plan["scheduled_end"])
        if end <= start:
            return ["schedule_window_invalid"]
        blockers: list[str] = []
        for other in self.s.lessons.values():
            if other.lesson_id == exclude_lesson or other.delivery is not None or not other.plan:
                continue
            body = other.plan["payload"]
            o_start, o_end = timeutil.parse(body["scheduled_at"]), timeutil.parse(body["scheduled_end"])
            if start < o_end and o_start < end:
                if body["mentor_id"] == plan["mentor_id"]:
                    blockers.append("mentor_overbooked")
                if set(body.get("required_resources", [])) & set(plan.get("required_resources", [])):
                    blockers.append("resource_busy")
        return blockers

    def plan_lesson(
        self, lesson_id: str, series_id: str, mentor_id: str, class_ref: str, grade: str,
        scheduled_at: str, scheduled_end: str, required_grants: list[str],
        required_resources: list[str], occurred_at: str, event_id: str | None = None,
    ) -> Decision:
        plan = {
            "series_id": series_id, "mentor_id": mentor_id, "class_ref": class_ref,
            "grade": grade, "scheduled_at": scheduled_at, "scheduled_end": scheduled_end,
            "required_grants": list(required_grants),
            "required_resources": list(required_resources),
        }
        blockers = self.readiness(plan)
        if blockers:
            return Decision(False, "not_ready_to_open", blockers)
        events = [self._append(
            "LESSON_PLANNED", "lesson_session", lesson_id, occurred_at,
            {"lesson_id": lesson_id, **plan}, event_id,
        )]
        for resource_id in required_resources:
            events.append(self._append(
                "RESOURCE_BLOCKED", "lesson_session", lesson_id, occurred_at,
                {
                    "resource_id": resource_id, "start": scheduled_at, "end": scheduled_end,
                    "lesson_id": lesson_id, "mentor_id": mentor_id,
                },
            ))
        return Decision(True, "lesson_planned", events=events)

    def reschedule_lesson(
        self, lesson_id: str, scheduled_at: str, scheduled_end: str,
        required_resources: list[str], occurred_at: str,
    ) -> Decision:
        """临时调课：释放旧占用，按新时间重新验证资质与授权窗口。"""
        lesson = self.s.lessons.get(lesson_id)
        if lesson is None or not lesson.plan:
            return Decision(False, "lesson_not_found", [f"课次不存在: {lesson_id}"])
        if lesson.delivery is not None:
            return Decision(False, "lesson_already_delivered", ["已完成的课次不能调课"])
        old = lesson.plan["payload"]
        new_plan = {
            "series_id": old["series_id"], "mentor_id": old["mentor_id"],
            "class_ref": old["class_ref"], "grade": old["grade"],
            "scheduled_at": scheduled_at, "scheduled_end": scheduled_end,
            "required_grants": list(old["required_grants"]),
            "required_resources": list(required_resources),
        }
        # 调课按新时间重新验证全部开课条件（授权过期即在新时间被拦截），
        # 排期冲突计算时排除课次自身的旧占用。先验证、后落事件，阻断不产生副作用。
        blockers = self.readiness(new_plan, exclude_lesson=lesson_id)
        if blockers:
            return Decision(False, "reschedule_blocked", blockers)
        events: list[Mapping[str, Any]] = []
        for block in lesson.block_events:
            if not self._block_still_active(block):
                continue
            body = block["payload"]
            events.append(self._append(
                "RESOURCE_FREED", "lesson_session", lesson_id, occurred_at,
                {
                    "resource_id": body["resource_id"], "start": body["start"], "end": body["end"],
                    "lesson_id": lesson_id, "replaces_block_id": block["event_id"],
                },
            ))
        events.append(self._append(
            "LESSON_PLANNED", "lesson_session", lesson_id, occurred_at,
            {"lesson_id": lesson_id, **new_plan},
        ))
        for resource_id in required_resources:
            events.append(self._append(
                "RESOURCE_BLOCKED", "lesson_session", lesson_id, occurred_at,
                {
                    "resource_id": resource_id, "start": scheduled_at, "end": scheduled_end,
                    "lesson_id": lesson_id, "mentor_id": old["mentor_id"],
                },
            ))
        return Decision(True, "lesson_rescheduled", events=events)

    def _block_still_active(self, block: Mapping[str, Any]) -> bool:
        bid = block["event_id"]
        return not any(
            f["payload"].get("replaces_block_id") == bid for f in self.s.events if f["event_type"] == "RESOURCE_FREED"
        )

    @staticmethod
    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    # ---- 送达与交付 ----------------------------------------------------

    def _receipt_key(self, kind: str, body: Mapping[str, Any]) -> tuple[str, str, str, str]:
        return (kind, body["lesson_id"], body["class_ref"], body["scheduled_at"])

    def _find_receipt(self, key: tuple[str, str, str, str]) -> Mapping[str, Any] | None:
        lesson = self.s.lessons.get(key[1])
        if lesson is None:
            return None
        for receipt in lesson.receipts:
            body = receipt.event["payload"]
            if (receipt.kind, body["lesson_id"], body["class_ref"], body["scheduled_at"]) == key:
                return receipt.event
        return None

    def record_attendance(
        self, lesson_id: str, class_ref: str, scheduled_at: str,
        attended_student_ids: list[str], occurred_at: str, event_id: str | None = None,
    ) -> Decision:
        key = ("attendance", lesson_id, class_ref, scheduled_at)
        if self._find_receipt(key) is not None:
            return Decision(True, "duplicate_receipt_ignored", idempotent=True)
        event = self._append(
            "ATTENDANCE_SENT", "lesson_session", lesson_id, occurred_at,
            {
                "lesson_id": lesson_id, "class_ref": class_ref,
                "scheduled_at": scheduled_at,
                "attended_student_ids": list(attended_student_ids),
            },
            event_id,
        )
        return Decision(True, "attendance_recorded", events=[event])

    def record_artifact_receipt(
        self, lesson_id: str, class_ref: str, scheduled_at: str, artifact_count: int,
        occurred_at: str, event_id: str | None = None,
    ) -> Decision:
        key = ("artifact", lesson_id, class_ref, scheduled_at)
        if self._find_receipt(key) is not None:
            return Decision(True, "duplicate_receipt_ignored", idempotent=True)
        event = self._append(
            "ARTIFACT_RECEIPT_SENT", "lesson_session", lesson_id, occurred_at,
            {
                "lesson_id": lesson_id, "class_ref": class_ref,
                "scheduled_at": scheduled_at, "artifact_count": artifact_count,
            },
            event_id,
        )
        return Decision(True, "artifact_receipt_recorded", events=[event])

    def _receipt_mismatch(self, lesson_id: str) -> list[str]:
        """签到/成果回执与课次计划在班级或时间上不一致。"""
        lesson = self.s.lessons.get(lesson_id)
        if lesson is None or not lesson.plan:
            return ["lesson_not_planned"]
        plan = lesson.plan["payload"]
        problems: list[str] = []
        for receipt in lesson.receipts:
            body = receipt.event["payload"]
            if body["class_ref"] != plan["class_ref"]:
                problems.append(f"{receipt.kind}_class_mismatch")
            if body["scheduled_at"] != plan["scheduled_at"]:
                problems.append(f"{receipt.kind}_time_mismatch")
        return self._dedup(problems)

    def confirm_delivery(
        self, lesson_id: str, content_used: list[str],
        contribution_snapshot: Mapping[str, Any], evidence_hash: str,
        occurred_at: str, event_id: str | None = None,
    ) -> Decision:
        lesson = self.s.lessons.get(lesson_id)
        if lesson is None or not lesson.plan:
            return Decision(False, "lesson_not_found", [f"课次不存在: {lesson_id}"])
        if lesson.delivery is not None:
            return Decision(True, "already_delivered", idempotent=True)
        plan = lesson.plan["payload"]
        extra: list[Mapping[str, Any]] = []
        issues: list[str] = list(self._receipt_mismatch(lesson_id))
        for content_ref in content_used:
            grant = self._active_grant(plan["mentor_id"], content_ref, occurred_at)
            if grant is None:
                issues.append(f"content_used_without_grant:{content_ref}")
        event = self._append(
            "DELIVERY_CONFIRMED", "lesson_session", lesson_id, occurred_at,
            {
                "lesson_id": lesson_id, "content_used": list(content_used),
                "contribution_snapshot": dict(contribution_snapshot),
                "evidence_hash": evidence_hash,
            },
            event_id,
        )
        # 过期/越权内容进了课堂：先记下交付事实，再立整改义务，结算保持暂停。
        # 同一课次已有未闭环同类整改义务时不重复开立。
        bad = [i for i in issues if i.startswith("content_used_without_grant:")]
        if bad and not self._open_obligation(KIND_RECTIFY, lesson_id, {"reasons": bad}):
            extra.append(self._raise_obligation(
                KIND_RECTIFY, lesson_id, occurred_at,
                detail={"reasons": bad},
                obligation_id=self._next_obligation_id(KIND_RECTIFY, lesson_id),
            ))
        return Decision(not bad, "delivery_confirmed" if not bad else "delivery_with_infractions",
                        issues=issues, events=[event, *extra])

    def give_feedback(
        self, lesson_id: str, issue_codes: list[str], occurred_at: str,
    ) -> Decision:
        if not issue_codes:
            return Decision(False, "empty_feedback", ["反馈必须包含至少一个问题代码"])
        events = [self._append(
            "QUALITY_FEEDBACK_GIVEN", "lesson_session", lesson_id, occurred_at,
            {"lesson_id": lesson_id, "issue_codes": list(issue_codes)},
        )]
        detail = {"issue_codes": list(issue_codes)}
        if not self._open_obligation(KIND_RECTIFY, lesson_id, detail):
            events.append(self._raise_obligation(
                KIND_RECTIFY, lesson_id, occurred_at, detail=detail,
                obligation_id=self._next_obligation_id(KIND_RECTIFY, lesson_id),
            ))
        return Decision(True, "feedback_recorded", events=events)

    # ---- 同意与公开成果 ------------------------------------------------

    def grant_consent(
        self, consent_id: str, student_id: str, class_ref: str, guardian_contact: str,
        scope: list[str], valid_from: str, valid_to: str | None, occurred_at: str,
    ) -> Decision:
        event = self._append(
            "STUDENT_CONSENT_GRANTED", "student_consent", consent_id, occurred_at,
            {
                "student_id": student_id, "class_ref": class_ref,
                "guardian_contact": guardian_contact, "scope": list(scope),
                "valid_from": valid_from, "valid_to": valid_to,
            },
        )
        return Decision(True, "consent_granted", events=[event])

    def record_approval(
        self, approval_id: str, artifact_ref: str, channel: str,
        approver_id: str, granted: bool, granted_at: str,
    ) -> Decision:
        if channel not in (APPROVAL_MENTOR, APPROVAL_SCHOOL):
            return Decision(False, "unsupported_channel", [f"批准渠道必须是 mentor/school: {channel}"])
        event = self._append(
            "APPROVAL_RECORDED", "approval", approval_id, granted_at,
            {
                "artifact_ref": artifact_ref, "channel": channel,
                "approver_id": approver_id, "granted": bool(granted),
                "granted_at": granted_at,
            },
        )
        return Decision(True, "approval_recorded", events=[event])

    def _approvals_for(self, artifact_id: str) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        for ev in self.s.approvals.values():
            body = ev["payload"]
            if body["artifact_ref"] == artifact_id and body["granted"]:
                result[body["channel"]] = ev
        return result

    def publish_artifact(
        self, artifact_id: str, lesson_id: str, student_ids: list[str],
        grants_used: list[str], contributors: list[Mapping[str, Any]],
        venue: str, fee_bearing: bool, published_at: str,
    ) -> Decision:
        lesson = self.s.lessons.get(lesson_id)
        if lesson is None or not lesson.plan:
            return Decision(False, "lesson_not_found", [f"课次不存在: {lesson_id}"])
        if lesson.delivery is None:
            return Decision(False, "lesson_not_delivered", ["成果只能来自已完成交付的课次"])
        if self.s.artifacts.get(artifact_id, {}).get("published") is not None:
            return Decision(True, "already_published", idempotent=True)
        plan = lesson.plan["payload"]
        mentor_id = plan["mentor_id"]
        issues: list[str] = []

        if self._agreement_is_narrowed(mentor_id, published_at):
            issues.append("agreement_narrowed_for_new_use")
        for student_id in student_ids:
            consent = self._active_consent(student_id, plan["class_ref"], venue, published_at)
            if consent is None:
                issues.append(f"consent_missing_or_expired:{student_id}")
        for grant_id in grants_used:
            grant_event = self.s.grants.get(grant_id)
            if not grant_event:
                issues.append(f"grant_unknown:{grant_id}")
                continue
            body = grant_event[-1]["payload"]
            if body["mentor_id"] != mentor_id:
                issues.append(f"grant_not_belong_to_mentor:{grant_id}")
            if not timeutil.window_covers(body.get("valid_from"), body.get("valid_to"),
                                          timeutil.parse(published_at)):
                issues.append(f"grant_expired:{grant_id}")
            if venue not in body["scope"]:
                issues.append(f"grant_scope_insufficient:{grant_id}")
        approvals = self._approvals_for(artifact_id) if fee_bearing else {}
        if fee_bearing:
            if APPROVAL_MENTOR not in approvals or APPROVAL_SCHOOL not in approvals:
                issues.append("independent_approvals_incomplete")
            elif approvals[APPROVAL_MENTOR]["payload"]["approver_id"] == approvals[APPROVAL_SCHOOL]["payload"]["approver_id"]:
                issues.append("approvals_not_independent")

        if issues:
            return Decision(False, "publication_blocked", issues)
        event = self._append(
            "ARTIFACT_PUBLISHED", "student_artifact", artifact_id, published_at,
            {
                "lesson_id": lesson_id, "student_ids": list(student_ids),
                "grants_used": list(grants_used),
                "contributors": [dict(c) for c in contributors],
                "venue": venue, "fee_bearing": bool(fee_bearing),
                "mentor_approval_id": approvals.get(APPROVAL_MENTOR, {}).get("event_id") if fee_bearing else None,
                "school_approval_id": approvals.get(APPROVAL_SCHOOL, {}).get("event_id") if fee_bearing else None,
                "published_at": published_at,
            },
        )
        return Decision(True, "artifact_published", events=[event])

    def _active_consent(
        self, student_id: str, class_ref: str, venue: str, at: str
    ) -> Mapping[str, Any] | None:
        moment = timeutil.parse(at)
        for ev in self.s.consents.get(student_id, []):
            body = ev["payload"]
            if body["class_ref"] != class_ref:
                continue
            if venue not in body["scope"]:
                continue
            if timeutil.window_covers(body.get("valid_from"), body.get("valid_to"), moment):
                return ev
        return None

    def delist_artifact(self, artifact_id: str, reason: str, occurred_at: str) -> Decision:
        record = self.s.artifacts.get(artifact_id)
        if record is None or record.get("published") is None:
            return Decision(False, "artifact_not_found", [f"成果不存在或未发布: {artifact_id}"])
        if record.get("delisted") is not None:
            return Decision(True, "already_delisted", idempotent=True)
        event = self._append(
            "ARTIFACT_DELISTED", "student_artifact", artifact_id, occurred_at,
            {"artifact_id": artifact_id, "reason": reason},
        )
        return Decision(True, "artifact_delisted", events=[event])

    # ---- 收窄与退出 ----------------------------------------------------

    def _raise_narrowing_obligations(
        self, agreement_id: str, at: str
    ) -> list[Mapping[str, Any]]:
        """收窄只面向未来：新用途在决策处硬拦；已公开成果产生下架/补署名义务。"""
        grant_ids = {
            gid for gid, history in self.s.grants.items()
            if history[-1]["payload"]["agreement_id"] == agreement_id
        }
        events: list[Mapping[str, Any]] = []
        for artifact_id, record in self.s.artifacts.items():
            published = record.get("published")
            if published is None or record.get("delisted") is not None:
                continue
            if not grant_ids.intersection(published["payload"]["grants_used"]):
                continue
            if not self.s.open_obligations(context_ref=f"artifact:{artifact_id}"):
                events.append(self._raise_obligation(
                    KIND_DELIST_OR_ATTRIBUTE, f"artifact:{artifact_id}", at,
                ))
        return events

    def exit_mentor(
        self, mentor_id: str, handover_notes_ref: str, series_affected: list[str],
        occurred_at: str,
    ) -> Decision:
        mentor = self.s.mentors.get(mentor_id)
        if mentor is None:
            return Decision(False, "mentor_not_found", [f"导师不存在: {mentor_id}"])
        events = [self._append(
            "MENTOR_EXITED", "mentor_profile", mentor_id, occurred_at,
            {
                "mentor_id": mentor_id, "handover_notes_ref": handover_notes_ref,
                "future_blocked": True, "series_affected": list(series_affected),
            },
        )]
        events.append(self._raise_obligation(
            KIND_HANDOVER, f"mentor:{mentor_id}", occurred_at,
            detail={"handover_notes_ref": handover_notes_ref,
                    "series_affected": list(series_affected)},
        ))
        return Decision(True, "mentor_exited", events=events)

    def _open_obligation(
        self, kind: str, context_ref: str, detail_subset: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        for ob in self.s.open_obligations(context_ref=context_ref):
            if ob["payload"]["kind"] != kind:
                continue
            if detail_subset is not None and not all(
                ob["payload"].get("detail", {}).get(k) == v
                for k, v in detail_subset.items()
            ):
                continue
            return ob
        return None

    def _next_obligation_id(self, kind: str, context_ref: str) -> str:
        base = f"obl-{kind}-{context_ref.replace(':', '-')}"
        existing = set(self.s.obligations)
        if base not in existing:
            return base
        seq = 2
        while f"{base}-{seq}" in existing:
            seq += 1
        return f"{base}-{seq}"

    def _raise_obligation(
        self, kind: str, context_ref: str, at: str, detail: Mapping[str, Any] | None = None,
        obligation_id: str | None = None,
    ) -> Mapping[str, Any]:
        days = {
            KIND_RECTIFY: self.policy.rectify_days,
            KIND_DELIST_OR_ATTRIBUTE: self.policy.rectify_days,
            KIND_HANDOVER: self.policy.handover_days,
        }[kind]
        due = (timeutil.parse(at) + timedelta(days=days)).isoformat()
        obligation_id = obligation_id or f"obl-{kind}-{context_ref.replace(':', '-')}"
        return self._append(
            "OBLIGATION_RAISED", "obligation", obligation_id, at,
            {
                "kind": kind, "due_at": due, "context_ref": context_ref,
                "detail": dict(detail or {}),
            },
        )

    def close_obligation(self, obligation_id: str, resolution: str, occurred_at: str) -> Decision:
        ob = self.s.obligations.get(obligation_id)
        if ob is None or ob.get("raised") is None:
            return Decision(False, "obligation_not_found", [f"义务不存在: {obligation_id}"])
        if ob.get("closed") is not None:
            return Decision(True, "already_closed", idempotent=True)
        event = self._append(
            "OBLIGATION_CLOSED", "obligation", obligation_id, occurred_at,
            {"obligation_id": obligation_id, "resolution": resolution},
        )
        return Decision(True, "obligation_closed", events=[event])

    # ---- 费用结算 ------------------------------------------------------

    def settlement_holds(self, lesson_id: str) -> list[str]:
        lesson = self.s.lessons.get(lesson_id)
        if lesson is None or not lesson.plan:
            return ["lesson_not_found"]
        holds: list[str] = []
        if lesson.delivery is None:
            holds.append("delivery_missing")
        holds.extend(self._receipt_mismatch(lesson_id))
        plan = lesson.plan["payload"]
        mentor_id = plan["mentor_id"]
        # 收窄不溯及已完成课程：交付在收窄之前完成的，结算依据保留；
        # 只处理下架/补署名义务（按 artifact 维度跟踪），不扣课酬。
        if (self.s.mentors.get(mentor_id) or {}).get("exit") is not None:
            exit_at = self.s.mentors[mentor_id]["exit"]["occurred_at"]
            if lesson.delivery and timeutil.parse(exit_at) <= timeutil.parse(lesson.delivery["occurred_at"]):
                holds.append("mentor_exited_before_delivery")
        # 未闭环的整改义务暂停结算
        if self.s.open_obligations(context_ref=lesson_id):
            holds.append("open_obligation")
        # 参与人不一致：公开成果署名学生不在签到名单内
        attendance = next(
            (r.event["payload"] for r in lesson.receipts if r.kind == "attendance"), None
        )
        if attendance is not None:
            present = set(attendance["attended_student_ids"])
            for record in self.s.artifacts.values():
                published = record.get("published")
                if published and published["payload"]["lesson_id"] == lesson_id:
                    unknown = set(published["payload"]["student_ids"]) - present
                    if unknown:
                        holds.append("participant_mismatch:" + ",".join(sorted(unknown)))
        return self._dedup(holds)

    def amount_due(self, lesson_id: str) -> float:
        lesson = self.s.lessons.get(lesson_id)
        if lesson is None or not lesson.plan:
            raise ServiceError(f"课次不存在: {lesson_id}")
        series_id = lesson.plan["payload"]["series_id"]
        history = self.s.fee_policies.get(series_id)
        if not history:
            raise ServiceError(f"课程系列缺少费用政策: {series_id}")
        policy = history[-1]["payload"]
        amount = float(policy["base_fee_per_session"])
        for record in self.s.artifacts.values():
            published = record.get("published")
            if (
                published and published["payload"]["lesson_id"] == lesson_id
                and published["payload"]["fee_bearing"]
            ):
                amount += float(policy["artifact_royalty"])
        return round(amount, 2)

    def settle_fee(
        self, lesson_id: str, correlation_id: str, occurred_at: str,
        amount: float | None = None,
    ) -> Decision:
        lesson = self.s.lessons.get(lesson_id)
        if lesson is None or not lesson.plan:
            return Decision(False, "lesson_not_found", [f"课次不存在: {lesson_id}"])
        prior = self.s.correlations.get(correlation_id)
        if prior is not None:
            # 同一结算回执重发只计算一次
            return Decision(True, "duplicate_settlement_ignored", idempotent=True)
        holds = self.settlement_holds(lesson_id)
        if holds:
            return Decision(False, "settlement_on_hold", holds)
        mentor_id = lesson.plan["payload"]["mentor_id"]
        try:
            computed = self.amount_due(lesson_id)
        except ServiceError as exc:
            return Decision(False, "fee_policy_missing", [str(exc)])
        if amount is not None and round(float(amount), 2) != computed:
            return Decision(False, "amount_mismatch",
                            [f"申报金额 {amount} 与应付 {computed} 不一致"])
        series_id = lesson.plan["payload"]["series_id"]
        event = self._append(
            "FEE_SETTLED", "fee_account", series_id, occurred_at,
            {
                "series_id": series_id, "lesson_id": lesson_id,
                "mentor_id": mentor_id, "amount": computed,
                "correlation_id": correlation_id,
            },
        )
        return Decision(True, "fee_settled", events=[event])
