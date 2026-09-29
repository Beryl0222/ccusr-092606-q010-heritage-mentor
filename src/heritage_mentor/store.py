"""JSONL 仅追加事件存储。

每个事件写入前经过契约校验；进程重启后从文件完整重放，
业务状态、授权窗口和期限均由事件时间决定，不依赖进程生命周期。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from . import contracts
from .state import State, apply, replay


class StoreError(ValueError):
    pass


class EventStore:
    def __init__(self, path: str | Path, schema: Mapping[str, Any]):
        self.path = Path(path)
        self.schema = schema
        self.state = State()
        if self.path.exists():
            for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StoreError(f"{self.path}:{line_no} 不是合法 JSON: {exc}") from exc
                issues = contracts.validate_event(event, schema)
                if issues:
                    raise StoreError(
                        f"{self.path}:{line_no} 契约无效: "
                        + "; ".join(f"{i.field} {i.code}" for i in issues)
                    )
                try:
                    apply(self.state, event)
                except Exception as exc:  # noqa: BLE001 - 重放任何错误都应中止启动
                    raise StoreError(f"{self.path}:{line_no} 重放失败: {exc}") from exc

    def append(self, event: Mapping[str, Any]) -> None:
        """校验并追加事件；event_id 重复且内容一致时为自然幂等空操作。"""
        issues = contracts.validate_event(event, self.schema)
        if issues:
            raise StoreError("; ".join(f"{i.field} {i.code} {i.message}" for i in issues))
        if event["event_id"] in self.state.events_by_id:
            existing = self.state.events_by_id[event["event_id"]]
            if existing != event:
                raise StoreError(f"事件标识冲突: {event['event_id']}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        apply(self.state, event)

    def events(self) -> list[Mapping[str, Any]]:
        return list(self.state.events)
