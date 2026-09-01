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
    if hybrid_text.count(old_not_admitted) != 1:
        raise RuntimeError(
            "unexpected Patch4 not-admitted branch after method preservation"
        )
    hybrid_text = hybrid_text.replace(old_not_admitted, new_not_admitted, 1)

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
    if hybrid_text.count(old_peer_ack) != 1:
        raise RuntimeError(
            "unexpected Patch4 peer acknowledgement branch after preservation"
        )
    hybrid_text = hybrid_text.replace(old_peer_ack, new_peer_ack, 1)

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
