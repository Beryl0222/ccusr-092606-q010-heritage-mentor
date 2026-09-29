# 领域约定

定义学校、非遗导师、课程课次、学生成果、授权与费用履约之间的**可执行规则**。
仓库分两层：

- **契约层**（`contracts/`、`contracts.py`）：事件信封、事件—聚合归属、载荷必填与时区，不改写调用方输入。
- **履约层**（`domain.py`、`store.py`、`views.py`）：命令决策（`decide`）、事件归约（`apply`）、
  只追加 JSONL 日志与角色视图。所有规则都在决策阶段强制执行。

## 聚合与事件

| 聚合 | 事件 |
| --- | --- |
| `partnership_agreement` | `AGREEMENT_VERSIONED` |
| `mentor_profile` | `MENTOR_REGISTERED` |
| `mentor_grant` | `MENTOR_AUTHORIZED`、`GRANT_NARROWED` |
| `course` | `COURSE_PLANNED`、`PREPARATION_CONFIRMED` |
| `lesson_session` | `LESSON_SCHEDULED`、`LESSON_RESCHEDULED`、`LESSON_CANCELLED`、`DELIVERY_CONFIRMED`、`ATTENDANCE_RECORDED`、`QUALITY_FEEDBACK_GIVEN`、`RECTIFICATION_OPENED`、`RECTIFICATION_RESOLVED` |
| `student_consent` | `CONSENT_GRANTED`、`CONSENT_WITHDRAWN` |
| `student_artifact` | `ARTIFACT_REGISTERED`、`ARTIFACT_PUBLICATION_DECIDED`、`ARTIFACT_FEE_DECIDED`、`ATTRIBUTION_SUPPLEMENTED`、`ARTIFACT_TAKEDOWN_REQUESTED`、`ARTIFACT_TAKEDOWN_COMPLETED` |
| `payout` | `FEE_OBLIGATION_RAISED`、`PAYOUT_SETTLED` |
| `handoff` | `HANDOFF_REQUIRED`、`HANDOFF_COMPLETED` |
| `settlement_hold` | `SETTLEMENT_HOLD_PLACED`、`SETTLEMENT_HOLD_CLEARED` |

所有发生时间与期限字段必须携带时区；版本号在每个聚合内从 1 递增。
命令可携带 `idempotency_key`，事件可携带同名键用于去重。

## 角色边界

- **导师**：确认自己的技艺内容与贡献（`contribution_snapshot` 必须含导师贡献）。
- **学校**：决定课程与未成年人保护。建课必须有协作教师（课堂安全与课程目标责任），
  年级必须落在授权的 `grade_prerequisites` 内。
- **教务角色 `school_affairs`**：作出公开传播批准。
- **财务角色 `finance_bursar`**：作出费用批准。

公开传播且产生费用的成果需要**两个相互独立的批准**：角色不同，且 `approver_id`
不得为同一自然人；费用批准必须以公开批准为前提，两类批准都不可更改（只能一次）。

## 关键规则

### 开课与排期

- 协议必须有效、授权窗口必须覆盖课次起止，过期授权的课次排不进去。
- 同一导师时间窗重叠的课次拒绝（`mentor_overbooked`）。
- 专用设备按登记容量限制并发占用；第三个班级在容量 2 的窗口内预约会被拒绝
  （`resource_overbooked`）。设备容量以首次登记为准，申报不一致时报 `resource_capacity_conflict`。
- 调课会释放旧时间窗，并按同样规则重新校验。

### 示范与授课依据

- `DELIVERY_CONFIRMED` 记录当次实际使用的 `content_refs_used`、贡献快照、证据哈希，
  并冻结当时的授权依据快照 `grant_basis`。
- 授课时引用的内容若已过期、被收窄移除或从未授权，拒绝确认（`content_not_licensed`）。

### 授权收窄（只面向未来）

`GRANT_NARROWED` 携带 `effective_from` 与后续义务：

- 只阻止**未来课次与新用途**；已完成课次保留原依据（状态与 `grant_basis` 不变）。
- 已公开且使用了被移除内容的成果，自动产生 `ARTIFACT_TAKEDOWN_REQUESTED`；
  `follow_up_obligations` 还可要求补充署名（结算暂停）与资料交接（`HANDOFF_REQUIRED`）。
- 收窄在 `effective_from` 才生效；生效前成果仍可见，但溯源视图会标出待下架。

### 监护同意

- 参与与公开分别需要同意范围；同意有期限，可撤回。
- 撤回同意时，涉及该学生且仍公开的成果立即产生下架义务。

### 成果、署名与费用

- 成果必须来自已完成课次，登记时从授课贡献快照归集贡献者。
- 额外课酬生成前，必须覆盖全部贡献者署名，否则 `attribution_incomplete`。
- 课酬条件：课次已授课、签到达到 `quorum`、质量 `pass`、无未闭环整改、无暂停事项。
- 成果费用条件：公开与费用双批准齐备、署名完整、无暂停事项、未进入下架流程。
- 下架义务未履行完成前，对应成果费用不得结算（`takedown_pending`）。
- 支付金额必须与应付义务一致；义务只能支付一次。

### 幂等与暂停结算

- 签到/成果回执按 `receipt_ref` 去重：同一批次或重复发送只计一次；
  携带相同 `idempotency_key` 的命令重放不产生新事件。
- 同一回执编号再次到达，但**参与人、内容或时间**不一致时，挂起
  `SETTLEMENT_HOLD_PLACED`（`receipt_mismatch`），相关课酬/成果费用暂停，
  教务用 `resolve_hold` 核对闭环后恢复。

### 期限不因重启重新起算

备课截止、授权起止、整改期限、课酬期限与交接期限都以**带时区的绝对时间**
存放在事件载荷中，并通过 `deadline_anchor_event` 指向起算事件：

- 课酬锚定授课确认事件；成果费用锚定费用批准事件；
- 整改锚定质量反馈事件；交接锚定收窄等触发事件。

服务重启只重放日志还原状态，绝不重新起算任何期限。`view deadlines` 可逐项核对锚点与是否逾期。

## 命令清单（`type`）

`version_agreement`、`register_mentor`、`authorize_grant`、`narrow_grant`、
`plan_course`、`confirm_preparation`、`schedule_lesson`、`reschedule_lesson`、
`cancel_lesson`、`confirm_delivery`、`record_attendance`、`grant_consent`、
`withdraw_consent`、`register_artifact`、`decide_publication`、`decide_fee`、
`supplement_attribution`、`complete_takedown`、`raise_lesson_fee`、
`raise_artifact_fee`、`settle_payout`、`give_feedback`、`resolve_rectification`、
`complete_handoff`、`resolve_hold`。

各命令必填字段见 `domain.py` 中对应处理函数与 `contracts/domain.schema.json` 的载荷登记。

## 只读视图

- `readiness <course_id>`：教务查看课程/课次是否具备开课条件，逐项给出阻断原因。
- `mentor <mentor_id>`：导师核对授课贡献、应付/已付/暂停金额与待办交接。
- `guardian <guardian_ref>`：监护人只看到自己孩子的同意记录与相关公开成果。
- `provenance <artifact_id>`：还原任一成果来自哪次课、采用何种授权、
  各方实际贡献、当前授权是否仍可用、双批准人与尚未完成的交接。
- `deadlines`：全部备课/授权/整改/课酬/交接期限及其锚点事件。
