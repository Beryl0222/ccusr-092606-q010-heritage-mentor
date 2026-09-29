# 校地非遗导师履约簿

把非遗传承人入校授课涉及的协议、授权、年级前提、教师协作、课次、示范记录、
监护同意、成果署名、双独立批准、质量整改、费用与退出交接，落成一份**可执行的履约簿**：
命令驱动、事件溯源、规则强制，重启后状态与期限可完整还原。

## 目录

- `contracts/domain.schema.json`：事件、聚合归属与载荷字段契约。
- `data/sample.json`：单事件联调样例。
- `data/journal.sample.jsonl`：完整履约场景的事件日志（42 条，可直接重放）。
- `src/heritage_mentor/`
  - `contracts.py`：信封、时区、版本、事件—聚合、载荷校验。
  - `domain.py`：纯函数领域内核（命令 → 事件决策、事件 → 状态归约）。
  - `store.py`：只追加 JSONL 事件日志，重启重放，落盘前契约校验。
  - `views.py`：教务/导师/监护人视图、成果溯源与期限登记。
  - `cli.py`：命令行入口。
- `scripts/build_sample.py`：通过内核生成样例日志（保证每条事件都合法）。
- `tests/`：契约、领域规则与存储测试。
- `docs/domain.md`：领域对象、事件语义与规则清单。

## 核心规则（摘要）

- 导师时间窗与专用设备容量不可超额排期；调课释放旧窗口并重校验。
- 授课确认实际使用的工艺内容；过期或被收窄移除的内容不得继续使用。
- 授权收窄只阻止未来课次和新用途；已完成课程保留原依据，
  同时自动产生下架、补充署名或资料交接义务。
- 公开传播批准（教务）与费用批准（财务）必须角色不同、且为不同自然人。
- 签到/成果回执重复发送只计一次；内容、参与人或时间不一致则暂停结算，核对闭环后恢复。
- 备课、授权、整改、课酬与交接期限锚定在具体事件上，服务重启只重放日志，绝不重新起算。
- 监护人只能看到自己孩子相关授权；任一成果可溯源到课次、授权、贡献与未完成交接。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests scripts
```

## 重新生成样例日志

```bash
python3 scripts/build_sample.py
```

## 命令行

校验单个事件：

```bash
PYTHONPATH=src python3 -m heritage_mentor.cli validate contracts/domain.schema.json data/sample.json
```

把命令写入履约簿（拒绝时不落盘；重复幂等键只计一次）：

```bash
PYTHONPATH=src python3 -m heritage_mentor.cli apply data/journal.sample.jsonl <command.json>
```

重放日志并给出状态汇总（模拟服务重启）：

```bash
PYTHONPATH=src python3 -m heritage_mentor.cli replay data/journal.sample.jsonl
```

角色视图（`--now` 指定判定时点，默认当前 UTC）：

```bash
# 教务：课程是否具备开课条件
PYTHONPATH=src python3 -m heritage_mentor.cli view data/journal.sample.jsonl readiness CRS-jianzhi-1 --now "2026-09-29T12:00:00+08:00"
# 导师：贡献与应付费用
PYTHONPATH=src python3 -m heritage_mentor.cli view data/journal.sample.jsonl mentor MTR-chen --now "2026-09-29T12:00:00+08:00"
# 监护人：仅本人孩子
PYTHONPATH=src python3 -m heritage_mentor.cli view data/journal.sample.jsonl guardian GRD-301 --now "2026-09-29T12:00:00+08:00"
# 成果溯源：课次、授权、贡献、双批准、待办交接
PYTHONPATH=src python3 -m heritage_mentor.cli view data/journal.sample.jsonl provenance ART-0001 --now "2026-10-02T12:00:00+08:00"
# 全部期限及其锚点事件
PYTHONPATH=src python3 -m heritage_mentor.cli view data/journal.sample.jsonl deadlines --now "2026-09-29T12:00:00+08:00"
```

命令成功时输出 `valid` 或追加的事件；违反规则时向标准错误输出 `错误码\t中文说明` 并返回非零状态。
完整命令字段见 `docs/domain.md`。
