# 领域约定

定义学校、非遗导师、课程课次、成果授权和费用履约之间的领域事件。

聚合对象包括`partnership_agreement`、`mentor_grant`、`lesson_delivery`、`student_artifact`。事件类型包括`AGREEMENT_VERSIONED`、`MENTOR_AUTHORIZED`、`LESSON_SCHEDULED`、`DELIVERY_CONFIRMED`、`OBLIGATION_RAISED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `MENTOR_AUTHORIZED`：载荷还需包含 `scope`, `expires_at`。
- `LESSON_SCHEDULED`：载荷还需包含 `class_ref`, `resource_window`。
- `DELIVERY_CONFIRMED`：载荷还需包含 `contribution_snapshot`, `evidence_hash`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
