"""命令行入口。

用法：
  python -m heritage_mentor.cli validate <schema.json> <event.json>
  python -m heritage_mentor.cli apply    <store.jsonl> <command.json> [--schema ...]
  python -m heritage_mentor.cli ready    <store.jsonl> [--as-of 2026-10-01T00:00:00+08:00]
  python -m heritage_mentor.cli fees     <store.jsonl> --mentor M1
  python -m heritage_mentor.cli guardian <store.jsonl> --student S1
  python -m heritage_mentor.cli artifact <store.jsonl> --artifact A1
  python -m heritage_mentor.cli deadlines <store.jsonl>

command.json 形如：
  {"action": "plan_lesson", "args": {"lesson_id": "L1", ...}}
输出统一为 JSON；决策失败时退出码为 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from . import projections
from .contracts import validate_event
from .service import Ledger, Policy
from .store import EventStore, StoreError

_DEFAULT_SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def _load_schema(path: str | Path | None) -> Mapping[str, Any]:
    return json.loads(Path(path or _DEFAULT_SCHEMA).read_text(encoding="utf-8"))


def _open_ledger(path: str, schema_path: str | None, policy: Policy | None = None) -> Ledger:
    return Ledger(EventStore(path, _load_schema(schema_path)), policy=policy)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_validate(args: argparse.Namespace) -> int:
    schema = _load_schema(args.schema)
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def _cmd_apply(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.store, args.schema)
    command = json.loads(Path(args.command).read_text(encoding="utf-8"))
    action = getattr(ledger, command["action"], None)
    if not callable(action):
        print(f"未知动作: {command.get('action')}", file=sys.stderr)
        return 2
    try:
        decision = action(**command.get("args", {}))
    except StoreError as exc:
        _print_json({"ok": False, "code": "store_error", "issues": [str(exc)]})
        return 1
    except TypeError as exc:
        _print_json({"ok": False, "code": "bad_arguments", "issues": [str(exc)]})
        return 2
    _print_json(decision.as_dict())
    return 0 if decision.ok else 1


def _cmd_ready(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.store, args.schema)
    _print_json(projections.academic_view(ledger, as_of=args.as_of))
    return 0


def _cmd_fees(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.store, args.schema)
    _print_json(projections.mentor_view(ledger, args.mentor))
    return 0


def _cmd_guardian(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.store, args.schema)
    _print_json(projections.guardian_view(ledger, args.student))
    return 0


def _cmd_artifact(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.store, args.schema)
    _print_json(projections.artifact_lineage(ledger, args.artifact))
    return 0


def _cmd_deadlines(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.store, args.schema)
    _print_json(projections.deadlines_board(ledger))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heritage_mentor", description="校地非遗导师履约簿")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="校验单个事件契约")
    p.add_argument("schema")
    p.add_argument("event")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("apply", help="执行一个服务动作并追加事件")
    p.add_argument("store")
    p.add_argument("command")
    p.add_argument("--schema")
    p.set_defaults(func=_cmd_apply)

    p = sub.add_parser("ready", help="教务开课条件视图")
    p.add_argument("store")
    p.add_argument("--schema")
    p.add_argument("--as-of")
    p.set_defaults(func=_cmd_ready)

    p = sub.add_parser("fees", help="导师贡献与费用视图")
    p.add_argument("store")
    p.add_argument("--schema")
    p.add_argument("--mentor", required=True)
    p.set_defaults(func=_cmd_fees)

    p = sub.add_parser("guardian", help="监护人最小可见视图")
    p.add_argument("store")
    p.add_argument("--schema")
    p.add_argument("--student", required=True)
    p.set_defaults(func=_cmd_guardian)

    p = sub.add_parser("artifact", help="公开成果溯源")
    p.add_argument("store")
    p.add_argument("--schema")
    p.add_argument("--artifact", required=True)
    p.set_defaults(func=_cmd_artifact)

    p = sub.add_parser("deadlines", help="期限看板")
    p.add_argument("store")
    p.add_argument("--schema")
    p.set_defaults(func=_cmd_deadlines)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
