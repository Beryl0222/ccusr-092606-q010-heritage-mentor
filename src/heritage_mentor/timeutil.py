"""时间解析与窗口判断。所有输入时间必须带时区。"""

from __future__ import annotations

from datetime import datetime, timezone


def parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区: {value}")
    return dt


def now() -> datetime:
    return datetime.now(timezone.utc)


def window_covers(start: str | None, end: str | None, moment: datetime) -> bool:
    """半开区间 [start, end)；空端表示不限制。"""
    if start is not None and moment < parse(start):
        return False
    if end is not None and moment >= parse(end):
        return False
    return True
