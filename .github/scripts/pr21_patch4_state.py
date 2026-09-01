from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

SOURCE_FINAL = "20d0a6a42365b2b2351e1dca819022b6ec477b39"
TARGET = Path("gateway/hosted_room_driver.py")
TYPE_ALIASES = ("OwnerProcessState", "OwnerLiveness")
INSERTED_FUNCTIONS = (
    "_stop_fenced_inactive_rows",
    "reconcile_stop_fenced_inactive_tasks",
)


def _span(text: str, node: ast.AST) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    total = 0
    for line in lines:
        offsets.append(total)
        total += len(line)
    start_line = int(node.lineno)
    decorators = getattr(node, "decorator_list", None) or ()
    if decorators:
        start_line = min(start_line, *(int(item.lineno) for item in decorators))
    start = offsets[start_line - 1]
    end_line = int(node.end_lineno)
    end = offsets[end_line] if end_line < len(lines) else total
    return start, end


def _segment(text: str, node: ast.AST) -> str:
    start, end = _span(text, node)
    return text[start:end].rstrip() + "\n"


def _top_level_function(text: str, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one top-level function {name}, found {len(matches)}")
    return matches[0]


def _optional_top_level_function(text: str, name: str) -> ast.FunctionDef | None:
    matches = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) > 1:
        raise RuntimeError(f"duplicate top-level function {name}")
    return matches[0] if matches else None


def _assignment(text: str, name: str) -> ast.Assign | ast.AnnAssign:
    matches: list[ast.Assign | ast.AnnAssign] = []
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                matches.append(node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                matches.append(node)
    if len(matches) != 1:
        raise RuntimeError(f"expected one assignment {name}, found {len(matches)}")
    return matches[0]


def _has_assignment(text: str, name: str) -> bool:
    try:
        _assignment(text, name)
    except RuntimeError as exc:
        if "found 0" in str(exc):
            return False
        raise
    return True


def _insert_type_aliases(text: str, source: str) -> str:
    present = [_has_assignment(text, name) for name in TYPE_ALIASES]
    if all(present):
        return text
    if any(present):
        raise RuntimeError("partial owner-liveness type aliases already exist")

    clock = _assignment(text, "Clock")
    _, insert_at = _span(text, clock)
    aliases = "".join(_segment(source, _assignment(source, name)) for name in TYPE_ALIASES)
    merged = text[:insert_at] + aliases + text[insert_at:]
    ast.parse(merged, filename=str(TARGET))
    return merged


def _insert_functions_before_admission(text: str, source: str) -> str:
    for name in INSERTED_FUNCTIONS:
        if _optional_top_level_function(text, name) is not None:
            raise RuntimeError(f"top-level function {name} already exists")

    admission = _top_level_function(text, "admit_task")
    insert_at, _ = _span(text, admission)
    functions = "\n".join(
        _segment(source, _top_level_function(source, name)).rstrip()
        for name in INSERTED_FUNCTIONS
    ) + "\n\n"
    merged = text[:insert_at] + functions + text[insert_at:]
    ast.parse(merged, filename=str(TARGET))
    for name in INSERTED_FUNCTIONS:
        if ast.dump(
            _top_level_function(merged, name), include_attributes=False
        ) != ast.dump(_top_level_function(source, name), include_attributes=False):
            raise RuntimeError(f"inserted function differs: {name}")
    return merged


def build(current: str, source: str) -> str:
    text = _insert_type_aliases(current, source)
    text = _insert_functions_before_admission(text, source)
    ast.parse(text, filename=str(TARGET))
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(TARGET))
    parser.add_argument("--report")
    args = parser.parse_args()

    current = TARGET.read_text(encoding="utf-8")
    source = subprocess.check_output(
        ["git", "show", f"{SOURCE_FINAL}:{TARGET.as_posix()}"],
        text=True,
    )
    merged = build(current, source)
    Path(args.output).write_text(merged, encoding="utf-8")

    summary = {
        "source_final": SOURCE_FINAL,
        "type_aliases": list(TYPE_ALIASES),
        "inserted_functions": list(INSERTED_FUNCTIONS),
        "current_sha256": hashlib.sha256(current.encode("utf-8")).hexdigest(),
        "merged_sha256": hashlib.sha256(merged.encode("utf-8")).hexdigest(),
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
