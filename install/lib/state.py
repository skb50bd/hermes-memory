"""state helper for install/lib/state.sh

Usage from bash (this script is invoked as `python3 - STATE_FILE get/get_json/set/set_json path value`):
    python3 - state.py <state_file> <command> <path> [value]

Commands:
    get <path>                    — print the value as a plain string
    get_json <path>               — print the value as JSON
    set <path> <value>            — set a string value
    set_json <path> <json_value>  — set a structured (decoded) value
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(f"state::load: corrupt state file {path}: {exc}", file=sys.stderr)
        return {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def get_by_path(data: dict, path: str) -> Any:
    cur: Any = data
    for k in path.split(".") if path else []:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def set_by_path(data: dict, path: str, value: Any) -> None:
    keys = path.split(".") if path else []
    if not keys:
        raise ValueError("empty path")
    cur = data
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def main(argv: list[str]) -> int:
    # When invoked as `python3 - state.py ...`, argv[0] is the literal "-"
    # (the placeholder for stdin). Strip it.
    if argv and argv[0] == "-":
        argv = argv[1:]
    if len(argv) < 3:
        print("usage: state.py <file> <cmd> <path> [value]", file=sys.stderr)
        return 2
    state_file = Path(argv[0])
    cmd = argv[1]
    path = argv[2]
    extra = argv[3:]

    data = load(state_file)

    if cmd == "get":
        v = get_by_path(data, path)
        if v is None:
            return 0
        if isinstance(v, (dict, list)):
            print(json.dumps(v))
        else:
            print(v)
        return 0

    if cmd == "get_json":
        v = get_by_path(data, path)
        if v is None:
            return 0
        print(json.dumps(v))
        return 0

    if cmd == "set":
        if not extra:
            print("set requires a value", file=sys.stderr)
            return 2
        value = extra[0]
        set_by_path(data, path, value)
        save(state_file, data)
        return 0

    if cmd == "set_json":
        if not extra:
            print("set_json requires a JSON value", file=sys.stderr)
            return 2
        try:
            value = json.loads(extra[0])
        except json.JSONDecodeError as exc:
            print(f"set_json: bad JSON: {exc}", file=sys.stderr)
            return 2
        set_by_path(data, path, value)
        save(state_file, data)
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
