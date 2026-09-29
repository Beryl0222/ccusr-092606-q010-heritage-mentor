# 校地非遗导师履约簿

事件溯源的可执行履约簿：关联合作协议、导师资质与内容授权、年级前提、教师协作
责任、课次排期、示范交付、监护同意、成果引用、质量反馈、费用条件与退出交接。

## 目录

- `contracts/domain.schema.json`：聚合、事件、载荷必填字段与载荷时间字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/heritage_mentor/`
  - `contracts.py`：信封/归属/时间静态校验；
  - `state.py`：事件归约（版本连续、event_id 幂等）；
  - `store.py`：JSONL 仅追加存储，重启完整重放；
  - `service.py`：领域决策（开课条件、超额排期、送达比对、独立批准、
    收窄不溯及、义务与费用）；
  - `projections.py`：教务/导师/监护人/成果溯源/期限视图；
  - `cli.py`：命令行入口。
- `tests/`：契约与全部业务规则测试（26 个）。
- `docs/domain.md`：领域语义与决策规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 命令行

```bash
# 校验单个事件
PYTHONPATH=src python3 -m heritage_mentor.cli validate contracts/domain.schema.json data/sample.json

# 执行一个服务动作（动作名即 Ledger 方法，args 为其关键字参数）
PYTHONPATH=src python3 -m heritage_mentor.cli apply data/ledger.jsonl command.json

# 角色视图
PYTHONPATH=src python3 -m heritage_mentor.cli ready     data/ledger.jsonl
PYTHONPATH=src python3 -m heritage_mentor.cli fees      data/ledger.jsonl --mentor M1
PYTHONPATH=src python3 -m heritage_mentor.cli guardian  data/ledger.jsonl --student S1
PYTHONPATH=src python3 -m heritage_mentor.cli artifact  data/ledger.jsonl --artifact A1
PYTHONPATH=src python3 -m heritage_mentor.cli deadlines data/ledger.jsonl
```

`command.json` 形如：

```json
{
  "action": "plan_lesson",
  "args": {
    "lesson_id": "L1", "series_id": "series-1", "mentor_id": "M1",
    "class_ref": "C3-1", "grade": "G3",
    "scheduled_at": "2026-10-10T10:00:00+08:00",
    "scheduled_end": "2026-10-10T11:00:00+08:00",
    "required_grants": ["paper-cut-basic"],
    "required_resources": ["craft-room"],
    "occurred_at": "2026-09-20T09:00:00+08:00"
  }
}
```

视图与成功决策输出 JSON；决策被阻断时退出码为 1，`issues` 给出阻断代码
（如 `grant_expired:<内容>`、`mentor_overbooked`、`resource_busy`、
`attendance_class_mismatch`、`independent_approvals_incomplete`、
`approvals_not_independent`、`agreement_narrowed`、`open_obligation`）。

## 规则要点

- 协议收窄只阻止未来课次与新用途；已完成课程保留原依据，已公开成果转为
  下架/补署名义务；
- 临时调课按新时间重验资质与授权，过期内容无法继续使用；
- 同一导师/专用设备同时段不可超额；回执重发只计一次，内容、参与人或时间
  不一致即暂停结算；
- 公开传播且产生费用的成果必须具备导师与学校两条相互独立的批准；
- 备课、授权、整改、交接、课酬期限全部锚定事件时间，重启不重新起算；
- 监护人视图只暴露本人孩子相关授权与署名成果。
