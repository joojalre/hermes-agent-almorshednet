#!/usr/bin/env bash
set -euo pipefail

: "${REPORT_DIR:?REPORT_DIR is required}"
mkdir -p "$REPORT_DIR"

cp tui_gateway/hosted_room_driver.py "$REPORT_DIR/current-runtime.py"
python .github/scripts/pr21_patch4_probe.py \
  | tee "$REPORT_DIR/full-runtime-build.txt"
cp tui_gateway/hosted_room_driver.py "$REPORT_DIR/full-runtime.py"
python .github/scripts/pr21_patch4_hybrid_v2.py \
  --baseline "$REPORT_DIR/current-runtime.py" \
  --candidate tui_gateway/hosted_room_driver.py \
  --output tui_gateway/hosted_room_driver.py \
  --report "$REPORT_DIR/runtime-hybrid-summary.json" \
  | tee "$REPORT_DIR/runtime-hybrid-build.txt"
cp tui_gateway/hosted_room_driver.py "$REPORT_DIR/runtime-hybrid.py"

cp gateway/hosted_room_driver.py "$REPORT_DIR/current-state.py"
python .github/scripts/pr21_patch4_state.py \
  --output gateway/hosted_room_driver.py \
  --report "$REPORT_DIR/state-summary.json" \
  | tee "$REPORT_DIR/state-build.txt"
cp gateway/hosted_room_driver.py "$REPORT_DIR/state-candidate.py"

cp tui_gateway/hosted_room_service.py "$REPORT_DIR/current-service.py"
python .github/scripts/pr21_patch4_service.py \
  --output tui_gateway/hosted_room_service.py \
  --report "$REPORT_DIR/service-summary.json" \
  | tee "$REPORT_DIR/service-build.txt"
cp tui_gateway/hosted_room_service.py "$REPORT_DIR/service-before-compat.py"

python .github/scripts/pr21_patch4_compat.py \
  --report "$REPORT_DIR/compat-summary.json" \
  | tee "$REPORT_DIR/compat-build.txt"
python .github/scripts/pr21_patch4_compat_v2.py \
  --report "$REPORT_DIR/compat-v2-summary.json" \
  | tee "$REPORT_DIR/compat-v2-build.txt"
python .github/scripts/pr21_patch4_compat_v3.py \
  --report "$REPORT_DIR/compat-v3-summary.json" \
  | tee "$REPORT_DIR/compat-v3-build.txt"
python .github/scripts/pr21_patch4_compat_v4.py \
  --report "$REPORT_DIR/compat-v4-summary.json" \
  | tee "$REPORT_DIR/compat-v4-build.txt"

cp tui_gateway/hosted_room_service.py "$REPORT_DIR/service-candidate.py"
cp tests/tui_gateway/test_hosted_room_service.py \
  "$REPORT_DIR/service-tests-candidate.py"
cp tests/gateway/test_hosted_rooms.py \
  "$REPORT_DIR/storage-tests-candidate.py"

python -m py_compile \
  gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_service.py \
  tests/tui_gateway/test_hosted_room_service.py \
  tests/gateway/test_hosted_rooms.py

git diff --check
git diff --stat | tee "$REPORT_DIR/candidate-stat.txt"
git diff -- \
  gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_service.py \
  tests/tui_gateway/test_hosted_room_service.py \
  tests/gateway/test_hosted_rooms.py \
  > "$REPORT_DIR/candidate.patch"

python - <<'PY' | tee "$REPORT_DIR/provenance.txt"
from pathlib import Path
import ast

report = Path("/tmp/pr21-patch4-probe-v17")
sources = {
    "current": (report / "current-runtime.py").read_text(encoding="utf-8"),
    "patch4": (report / "full-runtime.py").read_text(encoding="utf-8"),
    "hybrid": (report / "runtime-hybrid.py").read_text(encoding="utf-8"),
}


def method(source: str, name: str) -> str:
    matches = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1, (name, len(matches))
    return ast.dump(matches[0], include_attributes=False)


assert method(sources["hybrid"], "cancel") == method(sources["patch4"], "cancel")
print("cancel=patch4-exact")
assert method(sources["hybrid"], "_process_room") == method(
    sources["current"], "_process_room"
)
print("_process_room=current-exact")

runtime = Path("tui_gateway/hosted_room_driver.py").read_text(encoding="utf-8")
state = Path("gateway/hosted_room_driver.py").read_text(encoding="utf-8")
service = Path("tui_gateway/hosted_room_service.py").read_text(encoding="utf-8")
service_tests = Path("tests/tui_gateway/test_hosted_room_service.py").read_text(
    encoding="utf-8"
)
storage_tests = Path("tests/gateway/test_hosted_rooms.py").read_text(
    encoding="utf-8"
)
required = {
    "runtime": (
        "_register_process_admission(",
        "_unregister_process_admission(submission_key)",
        "Exact peer terminal status is already the durable Stop",
        "_complete_acknowledged_stop(binding, task, lease)",
        "foreign_owner_may_be_live = (",
        "transport.history(",
        'if not bool(getattr(exc, "retryable", False)):',
    ),
    "state": (
        "def reconcile_stop_fenced_inactive_tasks(",
        "def _stop_fenced_inactive_rows(",
    ),
    "service": (
        "import uuid",
        "if is_peer:",
        "existing = next(",
        "request = existing",
        "A later observation can include",
        "peer_actions = [",
        "driver.reconcile_stop_fenced_inactive_tasks(",
    ),
    "service_tests": (
        "process_generation=service.runtime.process_generation",
        "process_pid=service.runtime.process_pid",
        "process_start_time=service.runtime.process_start_time",
        '"payload": {"source_event_seq": 1}',
        '== {"choice": "once", "idempotent": False}',
    ),
    "storage_tests": (
        "single_event_pages = [",
        "budget = max(page_bytes(page) for page in single_event_pages) + 1",
        "created_at is serialized into every event",
    ),
}
sources_by_label = {
    "runtime": runtime,
    "state": state,
    "service": service,
    "service_tests": service_tests,
    "storage_tests": storage_tests,
}
for label, markers in required.items():
    source = sources_by_label[label]
    for marker in markers:
        assert marker in source, (label, marker)
        print(f"{label}:{marker}")
PY

uv run python -m pytest --help > "$REPORT_DIR/pytest-help.txt"
if grep -F -- "--file-retries" "$REPORT_DIR/pytest-help.txt"; then
  echo "unexpected file retry plugin is active" >&2
  exit 1
fi
echo "single_execution_no_retry_plugin=true" \
  | tee "$REPORT_DIR/retry-policy.txt"

uv run python -m pytest \
  tests/tui_gateway/test_hosted_room_service.py::test_stop_room_snapshots_tasks_before_status_transitions \
  tests/tui_gateway/test_hosted_room_service.py::test_demote_waits_for_exact_turn_stop_ack_before_authority_transfer \
  tests/tui_gateway/test_hosted_room_service.py::test_cross_process_pending_approval_requires_exact_generation_and_owner_consumes \
  tests/tui_gateway/test_hosted_room_service.py::test_headless_room_publishes_peer_member_reply_without_desktop_transport \
  tests/tui_gateway/test_hosted_room_service.py::test_peer_approval_is_scoped_visible_and_resolvable \
  tests/tui_gateway/test_hosted_room_service.py::test_local_room_approval_uses_the_exact_hidden_session \
  -q --tb=short | tee "$REPORT_DIR/previous-failures.log"

: > "$REPORT_DIR/race.log"
for attempt in $(seq 1 30); do
  echo "attempt=$attempt" | tee -a "$REPORT_DIR/race.log"
  uv run python -m pytest \
    tests/tui_gateway/test_hosted_room_driver_runtime.py::test_completion_wins_stop_race_after_attempt_lease_expires \
    -q --tb=short | tee -a "$REPORT_DIR/race.log"
done

: > "$REPORT_DIR/replay-page-repeat.log"
for attempt in $(seq 1 20); do
  echo "attempt=$attempt" | tee -a "$REPORT_DIR/replay-page-repeat.log"
  uv run python -m pytest \
    tests/gateway/test_hosted_rooms.py::test_room_log_pages_are_bounded_by_serialized_event_bytes \
    -q --tb=short | tee -a "$REPORT_DIR/replay-page-repeat.log"
done

uv run python -m pytest \
  tests/tui_gateway/test_hosted_room_driver_runtime.py::test_peer_terminal_status_acknowledges_durable_stop_on_retry \
  tests/tui_gateway/test_hosted_room_driver_runtime.py::test_peer_terminal_status_must_match_exact_task_attempt \
  -q --tb=short | tee "$REPORT_DIR/peer-stop.log"

uv run python -m pytest \
  tests/tui_gateway/test_hosted_room_driver_runtime.py \
  -q --tb=short | tee "$REPORT_DIR/runtime.log"

uv run python -m pytest \
  tests/tui_gateway/test_hosted_room_service.py \
  -q --tb=short | tee "$REPORT_DIR/service.log"

uv run python -m pytest \
  tests/gateway/test_hosted_room_gateway_lifecycle.py \
  tests/gateway/test_hosted_room_worker_shutdown.py \
  tests/gateway/test_hosted_room_discussion.py \
  tests/tui_gateway/test_hosted_room_prompt_fence.py \
  tests/tui_gateway/test_hosted_room_server_rpc.py \
  tests/tui_gateway/test_groups_methods.py \
  tests/tui_gateway/test_groups_replication_methods.py \
  -q --tb=short | tee "$REPORT_DIR/integration.log"

uv run python -m pytest \
  tests/gateway/test_hosted_rooms.py \
  tests/gateway/test_hosted_room_storage_reservations.py \
  tests/gateway/test_hosted_room_driver.py \
  tests/gateway/test_hosted_room_driver_admission.py \
  tests/gateway/test_hosted_room_replicas.py \
  tests/gateway/test_hosted_room_replicas_patch3.py \
  -q --tb=short | tee "$REPORT_DIR/prior-patches.log"

uv run ruff check \
  gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_service.py \
  tests/tui_gateway/test_hosted_room_service.py \
  tests/gateway/test_hosted_rooms.py \
  .github/scripts/pr21_patch4_state.py \
  .github/scripts/pr21_patch4_hybrid_v2.py \
  .github/scripts/pr21_patch4_service.py \
  .github/scripts/pr21_patch4_compat.py \
  .github/scripts/pr21_patch4_compat_v2.py \
  .github/scripts/pr21_patch4_compat_v3.py \
  .github/scripts/pr21_patch4_compat_v4.py \
  | tee "$REPORT_DIR/ruff-check.log"

python -m compileall -q \
  gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_driver.py \
  tui_gateway/hosted_room_service.py \
  tests/tui_gateway/test_hosted_room_service.py \
  tests/gateway/test_hosted_rooms.py

git diff --check

echo "probe_v17=success" | tee "$REPORT_DIR/final-status.txt"
