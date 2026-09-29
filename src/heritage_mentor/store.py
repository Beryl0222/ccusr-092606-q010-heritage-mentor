"""只追加的 JSONL 事件日志。

日志是唯一事实来源：期限、幂等键和结算状态全部存放在事件里，
服务重启后重放日志即可还原，任何备课、授权、整改和课酬期限都不会
因为重启而重新起算。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import validate_event
from .domain import DomainError, LedgerState, apply_event, decide


class EventStore:
    def __init__(self, path: str | Path, schema: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.schema = schema
        self.state = LedgerState()
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if self.schema is not None:
                    issues = validate_event(event, self.schema)
                    if issues:
                        detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
                        raise DomainError(
                            "journal_event_invalid",
                            f"日志第 {line_no} 行事件违反契约：{detail}",
                        )
                apply_event(event, self.state)

    def append_decision(self, command: Mapping[str, Any]) -> Sequence[dict[str, Any]]:
        """决策、落盘并归约。

        命中幂等键时返回首次产生的事件但不会再次写盘或归约——
        重复发送只计算一次。
        """
        events = decide(command, self.state)
        new_events = [event for event in events if event["event_id"] not in self.state.event_ids]
        if not new_events:
            return new_events
        if self.schema is not None:
            for event in new_events:
                issues = validate_event(event, self.schema)
                if issues:
                    detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
                    raise DomainError("decision_event_invalid", f"拟写入事件违反契约：{detail}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for event in new_events:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + os.linesep)
                handle.flush()
                apply_event(event, self.state)
        return new_events

    def replay(self) -> LedgerState:
        """强制从日志重新归约（验证用）。"""
        self.state = LedgerState()
        self._load()
        return self.state
