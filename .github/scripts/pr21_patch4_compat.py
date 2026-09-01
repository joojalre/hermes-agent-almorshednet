from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path

MAIN_FINAL = "e6c06033a04d9745ed1efea97ce35401323dbfd7"
SERVICE = Path("tui_gateway/hosted_room_service.py")
SERVICE_TESTS = Path("tests/tui_gateway/test_hosted_room_service.py")


def git_show(ref: str, path: Path) -> str:
    return subprocess.check_output(
        ["git", "show", f"{ref}:{path.as_posix()}"],
        text=True,
    )


def class_node(text: str, name: str) -> ast.ClassDef:
    matches = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one class {name}, found {len(matches)}")
    return matches[0]


def class_method(text: str, class_name: str, method_name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in class_node(text, class_name).body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {class_name}.{method_name}, found {len(matches)}"
        )
    return matches[0]


def top_function(text: str, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one function {name}, found {len(matches)}")
    return matches[0]


def optional_top_function(text: str, name: str) -> ast.FunctionDef | None:
    matches = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) > 1:
        raise RuntimeError(f"duplicate function {name}")
    return matches[0] if matches else None


def span(text: str, node: ast.AST) -> tuple[int, int]:
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


def replace_class_method(
    text: str,
    *,
    class_name: str,
    method_name: str,
    replacement: str,
) -> str:
    node = class_method(text, class_name, method_name)
    start, end = span(text, node)
    merged = text[:start] + replacement.rstrip() + "\n\n" + text[end:].lstrip("\n")
    ast.parse(merged)
    return merged


def replace_top_function(text: str, old_name: str, replacement: str) -> str:
    node = top_function(text, old_name)
    start, end = span(text, node)
    merged = text[:start] + replacement.rstrip() + "\n\n" + text[end:].lstrip("\n")
    ast.parse(merged)
    return merged


def insert_before_function(text: str, anchor_name: str, block: str) -> str:
    node = top_function(text, anchor_name)
    start, _ = span(text, node)
    merged = text[:start] + block.rstrip() + "\n\n" + text[start:]
    ast.parse(merged)
    return merged


SET_PENDING_ACTION = '''    def _set_pending_action(
        self,
        room_id: str,
        member_id: str,
        action: Mapping[str, Any] | None,
    ) -> None:
        key = (room_id, member_id)
        is_peer = key in self.peer_routes
        if action is None:
            if not is_peer:
                driver.clear_member_approval_requests(
                    self.db_path,
                    room_id=room_id,
                    member_id=member_id,
                )
            with self._policy_lock:
                self._pending_actions.pop(key, None)
            return

        durable_action = {**action, "member_id": member_id}
        if is_peer:
            # Peer approvals are resolved through the scoped remote receipt.
            # They are intentionally not inserted into the local driver table,
            # whose foreign key belongs to a local admitted task.
            with self._policy_lock:
                self._pending_actions[key] = durable_action
            return

        task_id = str(action.get("task_id") or "")
        execution_generation = int(action.get("execution_generation") or 0)
        task = next(
            (
                candidate
                for candidate in driver.list_tasks(
                    self.db_path,
                    room_id=room_id,
                )
                if candidate["identity"].task_id == task_id
                and int(candidate.get("execution_generation") or 0)
                == execution_generation
            ),
            None,
        )
        if task is None:
            raise driver.InvalidTaskTransitionError(
                "approval request task is unavailable"
            )
        identity = task["identity"]

        # Only the latest local callback for this member remains actionable.
        # The durable table still records the exact task generation and frozen
        # member id, while replacement callbacks retire stale request ids.
        driver.clear_member_approval_requests(
            self.db_path,
            room_id=room_id,
            member_id=member_id,
        )
        request = driver.publish_approval_request(
            self.db_path,
            identity,
            execution_generation=execution_generation,
            member_id=member_id,
            request_id=str(action.get("request_id") or ""),
            session_id=str(action.get("session_id") or ""),
            action=durable_action,
            clock=time.time,
        )
        with self._policy_lock:
            self._pending_actions[key] = durable_action
        choice = request.get("choice")
        if choice not in {"once", "deny"}:
            return
        result = self.rpc.approve(
            session_id=str(request["session_id"]),
            request_id=str(request["request_id"]),
            choice=str(choice),
        )
        if not isinstance(result, Mapping) or not bool(result.get("resolved")):
            return
        if not driver.mark_approval_consumed(
            self.db_path,
            identity,
            execution_generation=int(request["execution_generation"]),
            member_id=member_id,
            request_id=str(request["request_id"]),
            choice=str(choice),
            clock=time.time,
        ):
            raise RuntimeError(
                "room approval decision changed before acknowledgement"
            )
        with self._policy_lock:
            self._pending_actions.pop(key, None)
        self.runtime.wakeup()
'''


LOCAL_APPROVAL_HELPER = '''def _seed_running_local_approval_task(
    service: HostedRoomService,
    *,
    task_id: str,
) -> driver.TaskIdentity:
    gateway_id = hosted_rooms.local_authority_gateway_id()
    hosted_rooms.create_room(
        service.db_path,
        room_id="room-1",
        name="Approval room",
        members=[
            {"member_id": "local", "profile": "local", "handle": "local"}
        ],
        authority_gateway_id=gateway_id,
    )
    identity = driver.TaskIdentity(
        "room-1",
        task_id,
        "thread-local-1",
        "turn-local-1",
    )
    lease = driver.acquire_lease(
        service.db_path,
        room_id="room-1",
        gateway_id=gateway_id,
        authority_epoch=1,
        process_generation="approval-test-process",
        ttl_seconds=30,
        clock=time.time,
    )
    driver.admit_task(
        service.db_path,
        identity,
        payload={
            "target_profile": "local",
            "target_member_id": "local",
            "prompt": "Run the approved local action.",
            "source_event_seq": 1,
        },
        clock=time.time,
    )
    attempt = driver.start_task(
        service.db_path,
        identity,
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    assert attempt.execution_generation == 1
    return identity
'''


def patch_service(text: str) -> str:
    merged = replace_class_method(
        text,
        class_name="HostedRoomService",
        method_name="_set_pending_action",
        replacement=SET_PENDING_ACTION,
    )
    ast.parse(merged, filename=str(SERVICE))
    required = (
        "if is_peer:",
        "approval request task is unavailable",
        "driver.clear_member_approval_requests(",
    )
    for marker in required:
        if marker not in merged:
            raise RuntimeError(f"Service compatibility marker missing: {marker}")
    return merged


def patch_tests(text: str, main_text: str) -> str:
    old_stop = '''        lambda _db, *, room_id, cancel_id, **_authority: {
            "room_id": room_id,
            "cancel_id": cancel_id,
        },
'''
    new_stop = '''        lambda _db, *, room_id, cancel_id, **_authority: {
            "room_id": room_id,
            "cancel_id": cancel_id,
            "seq": 1,
        },
'''
    if text.count(old_stop) != 1:
        raise RuntimeError("unexpected Stop fixture shape")
    text = text.replace(old_stop, new_stop, 1)

    if optional_top_function(text, "_seed_running_local_approval_task") is None:
        text = insert_before_function(
            text,
            "test_local_room_approval_uses_the_exact_hidden_session",
            LOCAL_APPROVAL_HELPER,
        )

    local_anchor = '''    service.runtime.rpc = rpc
    service._set_pending_action(
        "room-1",
        "local",
'''
    local_replacement = '''    service.runtime.rpc = rpc
    _seed_running_local_approval_task(
        service,
        task_id="task-local-1",
    )
    service._set_pending_action(
        "room-1",
        "local",
'''
    if text.count(local_anchor) != 1:
        raise RuntimeError("unexpected local approval test setup")
    text = text.replace(local_anchor, local_replacement, 1)

    stale_anchor = '''    service.runtime.rpc = rpc
    action = {
        "kind": "approval",
'''
    stale_replacement = '''    service.runtime.rpc = rpc
    _seed_running_local_approval_task(
        service,
        task_id="task-local-1",
    )
    action = {
        "kind": "approval",
'''
    if text.count(stale_anchor) != 1:
        raise RuntimeError("unexpected stale approval test setup")
    text = text.replace(stale_anchor, stale_replacement, 1)

    obsolete = "test_terminal_publication_recovers_legacy_member_prefix_at_normal_limit"
    replacement_name = "test_terminal_publication_reserves_the_whole_plan_before_append"
    source_node = top_function(main_text, replacement_name)
    source_start, source_end = span(main_text, source_node)
    source_function = main_text[source_start:source_end]
    text = replace_top_function(text, obsolete, source_function)

    ast.parse(text, filename=str(SERVICE_TESTS))
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    args = parser.parse_args()

    current_service = SERVICE.read_text(encoding="utf-8")
    current_tests = SERVICE_TESTS.read_text(encoding="utf-8")
    main_tests = git_show(MAIN_FINAL, SERVICE_TESTS)

    merged_service = patch_service(current_service)
    merged_tests = patch_tests(current_tests, main_tests)
    SERVICE.write_text(merged_service, encoding="utf-8")
    SERVICE_TESTS.write_text(merged_tests, encoding="utf-8")

    summary = {
        "main_final": MAIN_FINAL,
        "production_changes": [
            "separate_peer_approval_from_local_driver_rows",
            "recover_exact_local_task_identity_from_durable_state",
            "retire_replaced_local_approval_request",
        ],
        "test_changes": [
            "stop_fixture_returns_durable_seq",
            "local_approval_tests_seed_running_task",
            "replace_obsolete_legacy_terminal_fixture_with_final_correlation_test",
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
