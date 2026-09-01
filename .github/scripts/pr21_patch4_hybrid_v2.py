from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

import pr21_patch4_hybrid as base

PRESERVED_PATCH4_METHODS = (
    "cancel",
    "_execute_attempt",
    "_retry_stopping_tasks",
)


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"unexpected Patch4 {label} branch")
    return text.replace(old, new, 1)


def build_hybrid(baseline_text: str, candidate_text: str) -> str:
    """Keep Patch 4 fences while preserving current peer and queue contracts."""

    hybrid_text = base.build_hybrid(baseline_text, candidate_text)
    for method_name in PRESERVED_PATCH4_METHODS:
        hybrid_text = base._restore_method(candidate_text, hybrid_text, method_name)

    old_not_admitted = '''            if submit_attempted and bool(getattr(exc, "not_admitted", False)):
                if transport is self.rpc:
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
    hybrid_text = _replace_once(
        hybrid_text,
        old_not_admitted,
        new_not_admitted,
        label="not-admitted",
    )

    old_peer_ack = '''                if self._peer_stop_acknowledged(binding, task):
                    if not self._settle_stopping_completion(binding, task, lease):
                        self._complete_acknowledged_stop(binding, task, lease)
                    continue
'''
    new_peer_ack = '''                if self._peer_stop_acknowledged(binding, task):
                    # Exact peer terminal status is already the durable Stop
                    # acknowledgement. Do not read peer history afterward: an
                    # interrupted historical reply can look like a failed turn
                    # and overwrite the cancellation contract.
                    self._complete_acknowledged_stop(binding, task, lease)
                    continue
'''
    hybrid_text = _replace_once(
        hybrid_text,
        old_peer_ack,
        new_peer_ack,
        label="peer acknowledgement",
    )

    old_owner_gate = '''        if owner_state in {"alive", "unknown"} and not current_attempt_is_local:
            return False
'''
    new_owner_gate = '''        foreign_owner_may_be_live = (
            owner_state in {"alive", "unknown"} and not current_attempt_is_local
        )
'''
    hybrid_text = _replace_once(
        hybrid_text,
        old_owner_gate,
        new_owner_gate,
        label="foreign owner liveness",
    )

    old_missing_session = '''        if session is None:
            return True
'''
    new_missing_session = '''        if session is None:
            return not foreign_owner_may_be_live
'''
    hybrid_text = _replace_once(
        hybrid_text,
        old_missing_session,
        new_missing_session,
        label="missing local session acknowledgement",
    )

    old_inactive_session = '''        if not active:
            return True
'''
    new_inactive_session = '''        if not active:
            if self._info_acknowledges_peer_cancel(info, task):
                return True
            return not foreign_owner_may_be_live
'''
    hybrid_text = _replace_once(
        hybrid_text,
        old_inactive_session,
        new_inactive_session,
        label="inactive local session acknowledgement",
    )

    old_wait_info = '''            info = transport.info(
                profile=profile,
                session_id=session_id,
                source=ROOM_SESSION_SOURCE,
            )
            self._report_pending_action(task, session_id=session_id, info=info)
            remaining = max(0.0, deadline_monotonic - time.monotonic())
'''
    new_wait_info = '''            info = transport.info(
                profile=profile,
                session_id=session_id,
                source=ROOM_SESSION_SOURCE,
            )
            self._report_pending_action(task, session_id=session_id, info=info)
            if (
                transport is not self.rpc
                and not bool(info.get("active", info.get("running", False)))
            ):
                receipt = _find_terminal_receipt(
                    transport.history(
                        profile=profile,
                        session_id=session_id,
                        source=ROOM_SESSION_SOURCE,
                    ),
                    attempt.identity,
                    attempt.execution_generation,
                )
                if receipt is not None:
                    return receipt
            remaining = max(0.0, deadline_monotonic - time.monotonic())
'''
    hybrid_text = _replace_once(
        hybrid_text,
        old_wait_info,
        new_wait_info,
        label="inactive peer terminal harvest",
    )

    ast.parse(hybrid_text)
    return hybrid_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report")
    args = parser.parse_args()

    baseline_text = Path(args.baseline).read_text(encoding="utf-8")
    candidate_text = Path(args.candidate).read_text(encoding="utf-8")
    hybrid_text = build_hybrid(baseline_text, candidate_text)
    Path(args.output).write_text(hybrid_text, encoding="utf-8")

    summary = {
        "restored_current_methods": ["_process_room"],
        "preserved_patch4_methods": list(PRESERVED_PATCH4_METHODS),
        "compatibility_adjustments": [
            "allow_current_runtime_attempt_through_owner_liveness_gate",
            "probe_local_admission_before_interrupt_so_completion_wins",
            "retry_retryable_not_admitted_and_fail_proven_local_rejection",
            "complete_exact_peer_terminal_stop_without_history_reharvest",
            "allow_exact_interrupt_for_reachable_foreign_local_owner",
            "refuse_absence_reclaim_while_foreign_owner_may_be_live",
            "harvest_exact_inactive_peer_terminal_history",
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
