from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

SOURCE_FINAL = "20d0a6a42365b2b2351e1dca819022b6ec477b39"
TARGET = Path("tui_gateway/hosted_room_service.py")
REPLACED_METHODS = ("_append_room_status", "demote_room")
INSERTED_METHOD = "_finish_room_demotion"


def _class_node(text: str) -> ast.ClassDef:
    matches = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and node.name == "HostedRoomService"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one HostedRoomService, found {len(matches)}")
    return matches[0]


def _method(text: str, name: str) -> ast.AST:
    matches = [
        node
        for node in _class_node(text).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one HostedRoomService.{name}, found {len(matches)}")
    return matches[0]


def _optional_method(text: str, name: str) -> ast.AST | None:
    matches = [
        node
        for node in _class_node(text).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) > 1:
        raise RuntimeError(f"duplicate HostedRoomService.{name}")
    return matches[0] if matches else None


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


def _replace_method(text: str, source: str, name: str) -> str:
    target_node = _method(text, name)
    source_node = _method(source, name)
    start, end = _span(text, target_node)
    merged = (
        text[:start]
        + _segment(source, source_node).rstrip()
        + "\n\n"
        + text[end:].lstrip("\n")
    ).rstrip() + "\n"
    ast.parse(merged, filename=str(TARGET))
    if ast.dump(_method(merged, name), include_attributes=False) != ast.dump(
        source_node, include_attributes=False
    ):
        raise RuntimeError(f"replacement differs for HostedRoomService.{name}")
    return merged


def _insert_method_before(
    text: str,
    source: str,
    *,
    method_name: str,
    before_name: str,
) -> str:
    if _optional_method(text, method_name) is not None:
        raise RuntimeError(f"HostedRoomService.{method_name} already exists")
    source_node = _method(source, method_name)
    before_node = _method(text, before_name)
    start, _ = _span(text, before_node)
    merged = (
        text[:start]
        + _segment(source, source_node).rstrip()
        + "\n\n"
        + text[start:].lstrip("\n")
    ).rstrip() + "\n"
    ast.parse(merged, filename=str(TARGET))
    if ast.dump(
        _method(merged, method_name), include_attributes=False
    ) != ast.dump(source_node, include_attributes=False):
        raise RuntimeError(f"inserted method differs: {method_name}")
    return merged


def _patch_uuid_import(text: str) -> str:
    if "\nimport uuid\n" in text:
        return text
    old = "import time\n"
    if text.count(old) != 1:
        raise RuntimeError("unexpected HostedRoomService time import")
    merged = text.replace(old, old + "import uuid\n", 1)
    ast.parse(merged, filename=str(TARGET))
    return merged


def _patch_prepare_room(text: str) -> str:
    old = '''    def prepare_room(self, binding: HostedRoomBinding) -> None:
        with self._policy_lock:
            room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
'''
    new = '''    def prepare_room(self, binding: HostedRoomBinding) -> None:
        with self._policy_lock:
            pending_demotion = driver.pending_room_demotion(
                self.db_path,
                room_id=binding.room_id,
            )
            if pending_demotion is not None:
                self._finish_room_demotion(pending_demotion)
                return
            room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
            driver.reconcile_stop_fenced_inactive_tasks(
                self.db_path,
                room_id=binding.room_id,
                clock=self.runtime.clock,
            )
'''
    if text.count(old) != 1:
        raise RuntimeError("unexpected HostedRoomService.prepare_room prefix")
    merged = text.replace(old, new, 1)
    ast.parse(merged, filename=str(TARGET))
    return merged


def build(current: str, source: str) -> str:
    text = _patch_uuid_import(current)
    for name in REPLACED_METHODS:
        text = _replace_method(text, source, name)
    text = _insert_method_before(
        text,
        source,
        method_name=INSERTED_METHOD,
        before_name="prepare_room",
    )
    text = _patch_prepare_room(text)
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
        "import_additions": ["uuid"],
        "replaced_methods": list(REPLACED_METHODS),
        "inserted_method": INSERTED_METHOD,
        "prepare_room_additions": [
            "resume_pending_demotion",
            "reconcile_stop_fenced_inactive_tasks",
        ],
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
