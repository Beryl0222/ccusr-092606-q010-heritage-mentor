# 领域约定

定义学校、非遗导师、课程课次、成果授权和费用履约之间的领域事件与决策规则。
实现采用事件溯源：状态全部由 JSONL 事件流重放得到，重启不改写任何业务时间。

## 聚合与事件

| 聚合 | 事件 |
| --- | --- |
| `partnership_agreement` | `AGREEMENT_VERSIONED`（`narrowed=true` 表示授权收窄） |
| `mentor_profile` | `MENTOR_QUALIFIED`、`MENTOR_EXITED` |
| `content_grant` | `CONTENT_GRANTED`（`scope` + `valid_from/valid_to` 授权窗口） |
| `lesson_series` | `GRADE_PREREQUISITE_SET`、`TEACHER_ROLE_ASSIGNED`（safety / curriculum） |
| `lesson_session` | `LESSON_PLANNED`、`RESOURCE_BLOCKED/FREED`、`ATTENDANCE_SENT`、`ARTIFACT_RECEIPT_SENT`、`DELIVERY_CONFIRMED`、`QUALITY_FEEDBACK_GIVEN` |
| `student_consent` | `STUDENT_CONSENT_GRANTED`（监护同意，按班级 + 传播范围 + 窗口） |
| `student_artifact` | `ARTIFACT_PUBLISHED`、`ARTIFACT_DELISTED` |
| `approval` | `APPROVAL_RECORDED`（channel = `mentor` / `school`） |
| `obligation` | `OBLIGATION_RAISED`、`OBLIGATION_CLOSED` |
| `fee_account` | `FEE_POLICY_SET`、`FEE_SETTLED` |

所有时间必带时区；同一聚合 `version` 从 1 起严格递增；`event_id` 全局唯一，
重复提交同一事件为自然幂等空操作。

## 关键业务规则

### 开课条件（教务视图 `ready`）

排课与临时调课都必须同时满足：

1. 导师资质在上课时点有效，且导师未退出；
2. 合作协议存在且未被收窄（收窄只阻断收窄时点之后的未来课次）；
3. 每项 `required_grants` 在上课时点有效且范围包含 `in_class`；
4. 年级在课程系列登记的前提年级内；
5. 校内教师 `safety`（课堂安全）与 `curriculum`（课程目标）两角色同时在岗；
6. 同一时段同一导师不超额、专用设备不被其他课次占用。

临时调课先释放旧的设备占用（`RESOURCE_FREED`），再按**新时间**重验全部条件；
授权在新时间过期则拒绝调课，过期内容不会流入课堂。验证先于落事件，阻断无副作用。

### 送达、交付与暂停结算

- 签到/成果回执以 `(类型, 课次, 班级, 计划时间)` 为幂等键，重发只记一次；
- 回执的班级或时间与课次计划不一致 → 阻断代码 `*_class_mismatch / *_time_mismatch`，
  结算暂停；
- `DELIVERY_CONFIRMED` 使用了无有效授权的内容时，交付事实仍保留，
  但立即产生 `rectify` 整改义务并暂停结算；
- 公开成果署名学生不在签到名单内 → `participant_mismatch:*`，暂停结算；
- 质量反馈未闭环、整改义务未关闭同样暂停结算。

### 公开成果与独立批准

- 成果只能挂在**已交付**课次下；
- 每位署名学生在发布时点必须具备班级匹配、范围匹配、窗口有效的监护同意；
- 引用的每项授权必须属于该导师、窗口覆盖发布时点、范围覆盖发布渠道；
- `fee_bearing=true`（公开传播且产生费用）必须同时持有渠道为 `mentor` 与 `school`
  的两条批准，且两位批准人不得相同——批准相互独立。

### 收窄与退出：不溯及既往

协议收窄（`narrowed=true`）：

- **只阻止未来课次与新用途**：收窄时点之后的排课、发布在决策处硬拦；
- 已完成课程保留原依据，课酬正常结算；
- 对引用受影响授权的**已发布成果**自动产生 `delist_or_attribute` 义务
  （下架或补充署名）。

导师退出（`MENTOR_EXITED`）：未来课次一律阻断，并产生 `handover` 交接义务。

### 费用与期限锚定

- 应付金额 = 基础课酬 + 每个产生费用的成果的额外课酬（`artifact_royalty`）；
- `FEE_SETTLED` 以 `correlation_id` 去重，结算回执重发只计算一次；
- 申报金额与政策计算金额不一致时拒绝；
- 所有期限（备课、授权到期、整改、交接、课酬）锚定对应事件的 `occurred_at`，
  由策略天数推导。服务重启只重放事件，期限绝不重新起算。

## 角色视图

- 教务：`ready` —— 课次状态、阻断项、占用资源、未闭环义务；
- 导师：`fees --mentor` —— 每节课的内容/贡献快照、暂停原因、应付与已付金额；
- 监护人：`guardian --student` —— 仅本人孩子的授权与署名成果，不含他人信息、费用；
- 成果溯源：`artifact --artifact` —— 来自哪次课、采用哪些授权、各方贡献、
  双方批准、下架状态与尚未完成的交接/整改义务；
- 期限看板：`deadlines` —— 各类期限、锚定事件与到期日。
