"""命令行入口：契约校验、命令落账、重放与角色视图。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate_event
from .domain import DomainError, parse_dt
from .store import EventStore
from . import views


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    default = Path(__file__).resolve().parents[2] / "contracts/domain.schema.json"
    return _load_json(str(default))


def _now(flag: str | None) -> datetime:
    if flag:
        return parse_dt(flag)
    return datetime.now(timezone.utc)


def _usage() -> None:
    print(
        "用法:\n"
        "  python -m heritage_mentor.cli validate <schema.json> <event.json>\n"
        "  python -m heritage_mentor.cli apply <journal.jsonl> <command.json> [--schema <schema.json>]\n"
        "  python -m heritage_mentor.cli replay <journal.jsonl> [--schema <schema.json>]\n"
        "  python -m heritage_mentor.cli view <journal.jsonl> <readiness|mentor|guardian|provenance|deadlines> <ref> "
        "[--now <iso>] [--schema <schema.json>]",
        file=sys.stderr,
    )


def cmd_validate(args: list[str]) -> int:
    if len(args) != 2:
        _usage()
        return 2
    schema, event = _load_json(args[0]), _load_json(args[1])
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _split(args: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            if index + 1 >= len(args):
                raise ValueError(f"选项缺少值：{token}")
            options[token] = args[index + 1]
            index += 2
        else:
            positional.append(token)
            index += 1
    return positional, options


def cmd_apply(args: list[str]) -> int:
    try:
        positional, options = _split(args)
    except ValueError as error:
        print(f"bad_arguments	{error}", file=sys.stderr)
        return 2
    if len(positional) != 2:
        _usage()
        return 2
    journal, command_path = positional
    schema = _load_json(options["--schema"]) if "--schema" in options else _schema()
    command = _load_json(command_path)
    store = EventStore(journal, schema)
    try:
        events = store.append_decision(command)
    except DomainError as error:
        print(f"{error.code}	{error.message}", file=sys.stderr)
        return 1
    for event in events:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True))
    print(f"appended {len(events)} event(s) -> {journal}", file=sys.stderr)
    return 0


def cmd_replay(args: list[str]) -> int:
    try:
        positional, options = _split(args)
    except ValueError as error:
        print(f"bad_arguments	{error}", file=sys.stderr)
        return 2
    if len(positional) != 1:
        _usage()
        return 2
    schema = _load_json(options["--schema"]) if "--schema" in options else _schema()
    try:
        store = EventStore(positional[0], schema)
    except DomainError as error:
        print(f"{error.code}	{error.message}", file=sys.stderr)
        return 1
    state = store.state
    summary = {
        "events": len(state.events),
        "agreements": len(state.agreements),
        "mentors": len(state.mentors),
        "grants": len(state.grants),
        "courses": len(state.courses),
        "lessons": len(state.lessons),
        "consents": len(state.consents),
        "artifacts": len(state.artifacts),
        "obligations": len(state.obligations),
        "open_holds": sum(len(state.active_holds(ref)) for ref in state.holds),
        "handoffs": len(state.handoffs),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_view(args: list[str]) -> int:
    try:
        positional, options = _split(args)
    except ValueError as error:
        print(f"bad_arguments	{error}", file=sys.stderr)
        return 2
    if len(positional) not in (2, 3):
        _usage()
        return 2
    journal, view_name = positional[0], positional[1]
    ref = positional[2] if len(positional) == 3 else None
    if view_name != "deadlines" and ref is None:
        print("missing_ref	该视图需要提供引用标识", file=sys.stderr)
        _usage()
        return 2
    schema = _load_json(options["--schema"]) if "--schema" in options else _schema()
    store = EventStore(journal, schema)
    state = store.state
    now = _now(options.get("--now"))
    try:
        if view_name == "readiness":
            result = views.course_readiness(state, ref, now)
        elif view_name == "mentor":
            result = views.mentor_statement(state, ref, now)
        elif view_name == "guardian":
            result = views.guardian_view(state, ref, now)
        elif view_name == "provenance":
            result = views.artifact_provenance(state, ref, now)
        elif view_name == "deadlines":
            result = views.deadline_register(state, now)
        else:
            _usage()
            return 2
    except DomainError as error:
        print(f"{error.code}	{error.message}", file=sys.stderr)
        return 1
    except KeyError:
        print(f"not_found	引用不存在：{ref}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        _usage()
        return 2
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "validate":
        return cmd_validate(rest)
    if command == "apply":
        return cmd_apply(rest)
    if command == "replay":
        return cmd_replay(rest)
    if command == "view":
        return cmd_view(rest)
    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
