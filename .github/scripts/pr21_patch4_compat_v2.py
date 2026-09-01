from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

SERVICE = Path("tui_gateway/hosted_room_service.py")
SERVICE_TESTS = Path("tests/tui_gateway/test_hosted_room_service.py")


def _class_method(text: str, class_name: str, method_name: str) -> ast.FunctionDef:
    classes = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(classes) != 1:
        raise RuntimeError(f"expected one class {class_name}, found {len(classes)}")
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]
    if len(methods) != 1:
        raise RuntimeError(
            f"expected one {class_name}.{method_name}, found {len(methods)}"
        )
    return methods[0]


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


def _replace_method(
    text: str,
    *,
    class_name: str,
    method_name: str,
    replacement: str,
) -> str:
    start, end = _span(text, _class_method(text, class_name, method_name))
    merged = text[:start] + replacement.rstrip() + "\n\n" + text[end:].lstrip("\n")
    ast.parse(merged)
    return merged


def _replace_function(text: str, name: str, replacement: str) -> str:
    start, end = _span(text, _top_function(text, name))
    merged = text[:start] + replacement.rstrip() + "\n\n" + text[end:].lstrip("\n")
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
            # Peer approvals remain scoped to their remote receipt and are
            # intentionally excluded from the local task foreign key table.
            with self._policy_lock:
                self._pending_actions[key] = durable_action
            return

        task_id = str(action.get("task_id") or "")
        execution_generation = int(action.get("execution_generation") or 0)
        request_id = str(action.get("request_id") or "")
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

        existing = next(
            (
                request
                for request in driver.list_pending_approval_requests(
                    self.db_path,
                    room_id=room_id,
                )
                if request["identity"] == identity
                and int(request["execution_generation"])
                == execution_generation
                and request["member_id"] == member_id
                and request["request_id"] == request_id
            ),
            None,
        )
        if existing is None:
            # A genuinely newer request retires the previous member-scoped
            # callback. Re-observing the same request must preserve any durable
            # dashboard decision until the session owner acknowledges it.
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
            request_id=request_id,
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


STATUS = '''    def status(self, room_id: str | None = None) -> dict[str, Any]:
        runtime = self.runtime.status()
        runtime = {**runtime, "peer_routes": self._route_statuses(room_id)}
        if self._link_load_error:
            runtime = {**runtime, "link_load_error": self._link_load_error}
        if room_id is None:
            return runtime
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts = Counter(str(task["status"]) for task in tasks)
        pending_actions = [
            {
                "kind": "retry",
                "task_id": task["identity"].task_id,
            }
            for task in tasks
            if task["status"] in {"indeterminate", "deferred"}
        ]
        pending_actions.extend(
            {
                **request["action"],
                **(
                    {"decision": request["choice"]}
                    if request.get("choice") is not None
                    else {}
                ),
            }
            for request in driver.list_pending_approval_requests(
                self.db_path,
                room_id=room_id,
            )
        )
        with self._policy_lock:
            peer_actions = [
                dict(action)
                for (action_room_id, member_id), action in sorted(
                    self._pending_actions.items(),
                    key=lambda item: item[0],
                )
                if action_room_id == room_id
                and (action_room_id, member_id) in self.peer_routes
            ]
        pending_actions.extend(peer_actions)
        return {
            "running": runtime["running"],
            "working": bool(
                counts.get("running")
                or counts.get("queued")
                or counts.get("stopping")
            ),
            "blocked": room_id in runtime["blocked_rooms"]
            or bool(counts.get("indeterminate") or counts.get("stopping")),
            "counts": dict(counts),
            "pending_actions": pending_actions,
            "peer_routes": self._route_statuses(room_id),
        }
'''


STOP_FIXTURE_TEST = '''def test_stop_room_snapshots_tasks_before_status_transitions(monkeypatch, tmp_path):
    """One running task must not be counted again after it becomes stopping."""

    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    task = {
        "identity": identity,
        "status": "running",
        "cancel_id": None,
        "payload": {"source_event_seq": 1},
    }
    calls = []

    def listed(_db, *, room_id, status):
        assert room_id == "room-1"
        return [dict(task)] if task["status"] == status else []

    def cancel(_identity, *, cancel_id):
        calls.append(cancel_id)
        task["status"] = "stopping"
        task["cancel_id"] = cancel_id
        return dict(task)

    monkeypatch.setattr(driver, "list_tasks", listed)
    monkeypatch.setattr(
        hosted_rooms,
        "request_room_stop",
        lambda _db, *, room_id, cancel_id, **_authority: {
            "room_id": room_id,
            "cancel_id": cancel_id,
            "seq": 1,
        },
    )
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    hosted_rooms.create_room(
        service.db_path,
        room_id="room-1",
        name="Stop room",
        members=[],
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
    )
    service.runtime = SimpleNamespace(cancel=cancel, wakeup=lambda: None)

    assert service.stop_room("room-1", cancel_id="stop-1") == 1
    assert calls == ["stop-1"]
'''


LOCAL_APPROVAL_TEST = '''def test_local_room_approval_uses_the_exact_hidden_session(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    identity = _seed_running_local_approval_task(
        service,
        task_id="task-local-1",
    )
    action = {
        "kind": "approval",
        "task_id": "task-local-1",
        "execution_generation": 1,
        "session_id": "local-session",
        "request_id": "approval-local-1",
        "approval": {
            "description": "Run focused tests",
            "command": "pytest -q tests/focused",
            "choices": ["once", "deny"],
        },
    }
    service._set_pending_action("room-1", "local", action)

    assert service.approve_room_task(
        "room-1",
        member_id="local",
        task_id="task-local-1",
        execution_generation=1,
        choice="once",
        request_id="approval-local-1",
    ) == {"choice": "once", "idempotent": False}
    assert rpc.approvals == []

    # A dashboard process records only the durable decision. The process that
    # owns the hidden session consumes it on its next exact observation.
    service.runtime._report_pending_action(
        driver.get_task(service.db_path, identity),
        session_id="local-session",
        info={
            "pending_approval": {
                "request_id": "approval-local-1",
                "choices": ["once", "deny"],
            }
        },
    )
    assert rpc.approvals == [
        {
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "choice": "once",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []
'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    args = parser.parse_args()

    service_text = SERVICE.read_text(encoding="utf-8")
    tests_text = SERVICE_TESTS.read_text(encoding="utf-8")

    service_text = _replace_method(
        service_text,
        class_name="HostedRoomService",
        method_name="_set_pending_action",
        replacement=SET_PENDING_ACTION,
    )
    service_text = _replace_method(
        service_text,
        class_name="HostedRoomService",
        method_name="status",
        replacement=STATUS,
    )
    tests_text = _replace_function(
        tests_text,
        "test_stop_room_snapshots_tasks_before_status_transitions",
        STOP_FIXTURE_TEST,
    )
    tests_text = _replace_function(
        tests_text,
        "test_local_room_approval_uses_the_exact_hidden_session",
        LOCAL_APPROVAL_TEST,
    )

    ast.parse(service_text, filename=str(SERVICE))
    ast.parse(tests_text, filename=str(SERVICE_TESTS))
    SERVICE.write_text(service_text, encoding="utf-8")
    SERVICE_TESTS.write_text(tests_text, encoding="utf-8")

    required = {
        "service": (
            "Re-observing the same request must preserve any durable",
            "peer_actions = [",
            "and (action_room_id, member_id) in self.peer_routes",
        ),
        "tests": (
            '"payload": {"source_event_seq": 1}',
            '== {"choice": "once", "idempotent": False}',
            "driver.get_task(service.db_path, identity)",
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
            "preserve_same_request_dashboard_decision",
            "retire_only_genuinely_replaced_local_request",
            "expose_scoped_peer_pending_actions_in_status",
        ],
        "test_changes": [
            "add_stop_source_event_seq_fixture",
            "verify_durable_local_decision_before_session_owner_consumption",
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
