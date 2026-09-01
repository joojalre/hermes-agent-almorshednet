from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

SERVICE = Path("tui_gateway/hosted_room_service.py")
SERVICE_TESTS = Path("tests/tui_gateway/test_hosted_room_service.py")


def _top_function(text: str, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one function {name}, found {len(matches)}")
    return matches[0]


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


def _replace_function_body_text(
    text: str,
    *,
    name: str,
    old: str,
    new: str,
    label: str,
) -> str:
    node = _top_function(text, name)
    start, end = _span(text, node)
    function = text[start:end]
    if function.count(old) != 1:
        raise RuntimeError(
            f"{label}: expected one match in {name}, found {function.count(old)}"
        )
    function = function.replace(old, new, 1)
    merged = text[:start] + function + text[end:]
    ast.parse(merged)
    return merged


def patch_service(text: str) -> str:
    old = '''        request = driver.publish_approval_request(
            self.db_path,
            identity,
            execution_generation=execution_generation,
            member_id=member_id,
            request_id=request_id,
            session_id=str(action.get("session_id") or ""),
            action=durable_action,
            clock=time.time,
        )
'''
    new = '''        if existing is None:
            request = driver.publish_approval_request(
                self.db_path,
                identity,
                execution_generation=execution_generation,
                member_id=member_id,
                request_id=request_id,
                session_id=str(action.get("session_id") or ""),
                action=durable_action,
                clock=time.time,
            )
        else:
            # The request id is immutable. A later observation can include
            # richer transient metadata, but it must not mutate the durable
            # approval row or erase the dashboard decision already recorded.
            request = existing
'''
    if text.count(old) != 1:
        raise RuntimeError(
            f"approval publish block: expected one match, found {text.count(old)}"
        )
    merged = text.replace(old, new, 1)
    ast.parse(merged, filename=str(SERVICE))
    return merged


def patch_tests(text: str) -> str:
    old = '''        process_generation="worker",
        ttl_seconds=30,
'''
    new = '''        process_generation=service.runtime.process_generation,
        process_pid=service.runtime.process_pid,
        process_start_time=service.runtime.process_start_time,
        ttl_seconds=30,
'''
    return _replace_function_body_text(
        text,
        name="test_demote_waits_for_exact_turn_stop_ack_before_authority_transfer",
        old=old,
        new=new,
        label="demotion owner fixture",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    args = parser.parse_args()

    service_text = patch_service(SERVICE.read_text(encoding="utf-8"))
    tests_text = patch_tests(SERVICE_TESTS.read_text(encoding="utf-8"))
    SERVICE.write_text(service_text, encoding="utf-8")
    SERVICE_TESTS.write_text(tests_text, encoding="utf-8")

    required = {
        "service": (
            "A later observation can include",
            "request = existing",
        ),
        "tests": (
            "process_generation=service.runtime.process_generation",
            "process_pid=service.runtime.process_pid",
            "process_start_time=service.runtime.process_start_time",
        ),
    }
    for marker in required["service"]:
        if marker not in service_text:
            raise RuntimeError(f"missing Service marker: {marker}")
    for marker in required["tests"]:
        if marker not in tests_text:
            raise RuntimeError(f"missing test marker: {marker}")

    summary = {
        "production_changes": [
            "reuse_immutable_existing_approval_row_on_reobservation",
        ],
        "test_changes": [
            "demotion_task_uses_actual_runtime_owner_identity",
        ],
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
