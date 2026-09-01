from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

RESTORED_CURRENT_METHODS = (
    "_execute_attempt",
    "_process_room",
    "_retry_stopping_tasks",
)


def _class_method(text: str, method_name: str) -> ast.AST:
    tree = ast.parse(text)
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HostedRoomRuntime"
    ]
    if len(classes) != 1:
        raise RuntimeError("HostedRoomRuntime class is ambiguous")
    matches = [
        node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one HostedRoomRuntime.{method_name}, found {len(matches)}"
        )
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


def _restore_method(
    baseline_text: str,
    candidate_text: str,
    method_name: str,
) -> str:
    source_node = _class_method(baseline_text, method_name)
    target_node = _class_method(candidate_text, method_name)
    source_start, source_end = _span(baseline_text, source_node)
    target_start, target_end = _span(candidate_text, target_node)
    replacement = baseline_text[source_start:source_end].rstrip()
    merged = (
        candidate_text[:target_start]
        + replacement
        + "\n\n"
        + candidate_text[target_end:].lstrip("\n")
    ).rstrip() + "\n"
    ast.parse(merged)
    if ast.dump(
        _class_method(merged, method_name), include_attributes=False
    ) != ast.dump(source_node, include_attributes=False):
        raise RuntimeError(f"failed to restore HostedRoomRuntime.{method_name}")
    return merged


def build_hybrid(baseline_text: str, candidate_text: str) -> str:
    hybrid_text = candidate_text
    for method_name in RESTORED_CURRENT_METHODS:
        hybrid_text = _restore_method(baseline_text, hybrid_text, method_name)

    old_owner_gate = '''        owner_state = self._stopping_owner_state(task)
        if owner_state in {"alive", "unknown"}:
            return False
'''
    new_owner_gate = '''        owner_state = self._stopping_owner_state(task)
        with self._status_lock:
            current_attempt_is_local = (
                self._current_tasks.get(binding.room_id) == task["identity"]
            )
        if owner_state in {"alive", "unknown"} and not current_attempt_is_local:
            return False
'''
    if hybrid_text.count(old_owner_gate) != 1:
        raise RuntimeError("unexpected stopping owner gate shape")
    hybrid_text = hybrid_text.replace(old_owner_gate, new_owner_gate, 1)

    old_admission_interrupt = '''        if admission is not None:
            admitted_transport, admitted_profile, admitted_session_id = admission
            result = admitted_transport.interrupt(
                profile=admitted_profile,
                session_id=admitted_session_id,
                source=ROOM_SESSION_SOURCE,
                expected_task_id=task["identity"].task_id,
            )
            if result is None:
                return False
            if result.get("interrupted") is True or str(result.get("status") or "") in {
                "cancelled",
                "interrupted",
                "stopping",
            }:
                return True
            info = admitted_transport.info(
                profile=admitted_profile,
                session_id=admitted_session_id,
                source=ROOM_SESSION_SOURCE,
            )
            return not bool(info.get("active", info.get("running", False)))
'''
    new_admission_interrupt = '''        if admission is not None:
            admitted_transport, admitted_profile, admitted_session_id = admission
            info = admitted_transport.info(
                profile=admitted_profile,
                session_id=admitted_session_id,
                source=ROOM_SESSION_SOURCE,
            )
            active = bool(info.get("active", info.get("running", False)))
            current = state.get_task(self.db_path, task["identity"])
            if current["status"] in state.TERMINAL_STATUSES:
                return True
            if not active:
                return True
            if not _info_is_active_for(info, task["identity"], require_exact=True):
                return False
            result = admitted_transport.interrupt(
                profile=admitted_profile,
                session_id=admitted_session_id,
                source=ROOM_SESSION_SOURCE,
                expected_task_id=task["identity"].task_id,
            )
            if result is None:
                return False
            if result.get("interrupted") is True or str(result.get("status") or "") in {
                "cancelled",
                "interrupted",
                "stopping",
            }:
                return True
            info = admitted_transport.info(
                profile=admitted_profile,
                session_id=admitted_session_id,
                source=ROOM_SESSION_SOURCE,
            )
            return not bool(info.get("active", info.get("running", False)))
'''
    if hybrid_text.count(old_admission_interrupt) != 1:
        raise RuntimeError("unexpected local admission interrupt shape")
    hybrid_text = hybrid_text.replace(
        old_admission_interrupt,
        new_admission_interrupt,
        1,
    )

    old_not_admitted = '''            if submit_attempted and bool(getattr(exc, "not_admitted", False)):
                try:
                    state.requeue_not_admitted_task(
                        self.db_path,
                        attempt,
                        clock=self.clock,
                    )
                except (state.StaleLeaseError, state.StaleTaskError) as fence_exc:
                    self._drop_lease(binding.room_id)
                    self._ambiguous_rooms[binding.room_id] = attempt.lease.expires_at
                    self._record_error(
                        f"task {attempt.identity.task_id} not-admitted proof lost "
                        f"its fence: {fence_exc}"
                    )
                else:
                    delay = self._defer_unavailable_route(task)
                    self._record_error(
                        f"task {attempt.identity.task_id} was not admitted; "
                        f"queued for retry in {delay:g}s"
                    )
'''
    new_not_admitted = '''            if submit_attempted and bool(getattr(exc, "not_admitted", False)):
                if not bool(getattr(exc, "retryable", False)):
                    self._settle_failure_if_current(attempt, exc)
                else:
                    try:
                        state.requeue_not_admitted_task(
                            self.db_path,
                            attempt,
                            clock=self.clock,
                        )
                    except (state.StaleLeaseError, state.StaleTaskError) as fence_exc:
                        self._drop_lease(binding.room_id)
                        self._ambiguous_rooms[binding.room_id] = attempt.lease.expires_at
                        self._record_error(
                            f"task {attempt.identity.task_id} not-admitted proof lost "
                            f"its fence: {fence_exc}"
                        )
                    else:
                        delay = self._defer_unavailable_route(task)
                        self._record_error(
                            f"task {attempt.identity.task_id} was not admitted; "
                            f"queued for retry in {delay:g}s"
                        )
'''
    if hybrid_text.count(old_not_admitted) != 1:
        raise RuntimeError("unexpected not-admitted branch shape")
    hybrid_text = hybrid_text.replace(old_not_admitted, new_not_admitted, 1)

    ast.parse(hybrid_text)
    return hybrid_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report")
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    candidate_path = Path(args.candidate)
    output_path = Path(args.output)
    baseline_text = baseline_path.read_text(encoding="utf-8")
    candidate_text = candidate_path.read_text(encoding="utf-8")
    hybrid_text = build_hybrid(baseline_text, candidate_text)
    output_path.write_text(hybrid_text, encoding="utf-8")

    summary = {
        "restored_current_methods": list(RESTORED_CURRENT_METHODS),
        "compatibility_adjustments": [
            "keep_receipt_safe_source_cancel",
            "allow_current_runtime_attempt_through_owner_liveness_gate",
            "probe_local_admission_before_interrupt_so_completion_wins",
            "retry_retryable_not_admitted_and_fail_proven_local_rejection",
        ],
        "baseline_sha256": hashlib.sha256(
            baseline_text.encode("utf-8")
        ).hexdigest(),
        "candidate_sha256": hashlib.sha256(
            candidate_text.encode("utf-8")
        ).hexdigest(),
        "hybrid_sha256": hashlib.sha256(
            hybrid_text.encode("utf-8")
        ).hexdigest(),
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
