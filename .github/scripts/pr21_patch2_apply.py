from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

SOURCE_FINAL = "20d0a6a42365b2b2351e1dca819022b6ec477b39"
DRIVER_FILE = Path("gateway/hosted_room_driver.py")
CURRENT_TEST_FILE = Path("tests/gateway/test_hosted_room_driver.py")
FOCUSED_TEST_FILE = Path("tests/gateway/test_hosted_room_driver_admission.py")
TEMPORARY_FILES = (
    Path(".github/scripts/pr21_patch2_apply.py"),
    Path(".github/scripts/pr21_patch2_probe.py"),
    Path(".github/workflows/pr21-patch2-apply.yml"),
    Path(".github/workflows/pr21-patch2-probe.yml"),
)

SELECTED_NODES = frozenset({
    "DriverLease",
    "TaskAdmissionBlockedError",
    "_ADMISSION_BARRIER_COLUMNS",
    "_ADMISSION_BARRIER_PRIMARY_KEY",
    "_DEMOTION_INTENT_COLUMNS",
    "_DEMOTION_INTENT_PRIMARY_KEY",
    "_LEASE_COLUMNS",
    "_LEGACY_LEASE_COLUMNS",
    "_LEGACY_TASK_COLUMNS",
    "_OWNER_LEASE_COLUMNS",
    "_OWNER_TASK_COLUMNS",
    "_STARTUP_AUDITED_SCHEMAS",
    "_STARTUP_AUDIT_LOCK",
    "_TASK_COLUMNS",
    "_TASK_OPTIONAL_PAYLOAD_FIELDS",
    "_TASK_PAYLOAD_FIELDS",
    "_TASK_REQUIRED_PAYLOAD_FIELDS",
    "_audit_terminal_recovery_headroom_once",
    "_connect",
    "_create_admission_barrier_table",
    "_create_demotion_intent_table",
    "_create_task_table",
    "_demotion_intent_table_exists",
    "_ensure_admission_barrier",
    "_initialize_schema",
    "_lease_from_row",
    "_migrate_owner_identity_columns",
    "_optional_process_integer",
    "_raise_if_legacy_demotion_barrier_is_unrecoverable",
    "_raise_if_pending_demotion_intent_lacks_stop",
    "_raise_if_task_fenced",
    "_raise_if_terminal_recovery_headroom_is_unrecoverable",
    "_schema_is_current",
    "_task_from_row",
    "_task_payload",
    "_validate_schema",
    "acquire_lease",
    "admit_task",
    "begin_room_demotion",
    "block_room_admissions",
    "pending_room_demotion",
    "renew_lease",
    "requeue_deferred_task",
    "requeue_indeterminate_task",
    "start_task",
})
OBSOLETE_NODES = frozenset({
    "_TASK_PAYLOAD_OPTIONAL_FIELDS",
    "_TASK_PAYLOAD_REQUIRED_FIELDS",
})
PRESERVED_NODES = (
    "complete_task_cancel",
    "requeue_not_admitted_task",
    "resolve_indeterminate_cancellation",
)
TARGET_TESTS = (
    "test_task_admission_is_atomically_fenced_by_latest_stop",
    "test_durable_admission_barrier_blocks_new_tasks_idempotently",
    "test_room_demotion_intent_is_atomic_idempotent_and_conflict_checked",
    "test_demotion_intent_and_barrier_roll_back_when_atomic_stop_fails",
    "test_admission_barrier_is_scoped_to_superseded_authority_epoch",
    "test_admission_is_bounded_by_terminal_recovery_liability",
    "test_terminal_recovery_audit_runs_once_per_process_schema",
    "test_pre_admission_barrier_schema_is_migrated_without_losing_work",
    "test_pre_demotion_intent_schema_is_migrated_without_losing_work",
    "test_barrier_only_demotion_schema_fails_loudly_instead_of_wedging",
    "test_completed_legacy_demotion_barrier_does_not_block_schema_migration",
    "test_current_demotion_intent_without_atomic_stop_fails_loudly",
    "test_pre_owner_identity_schema_is_migrated_without_losing_work",
)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and process.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} ({process.returncode})\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return process


def _node_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (node.name,)
    if isinstance(node, ast.Assign):
        return tuple(
            target.id for target in node.targets if isinstance(target, ast.Name)
        )
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return (node.target.id,)
    return ()


def _line_offsets(text: str) -> tuple[list[int], int]:
    offsets: list[int] = []
    total = 0
    for line in text.splitlines(keepends=True):
        offsets.append(total)
        total += len(line)
    return offsets, total


def _span(text: str, node: ast.AST) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    offsets, total = _line_offsets(text)
    start_line = int(node.lineno)
    decorators = getattr(node, "decorator_list", None) or ()
    if decorators:
        start_line = min(start_line, *(int(decorator.lineno) for decorator in decorators))
    start = offsets[start_line - 1]
    end_line = int(node.end_lineno)
    end = offsets[end_line] if end_line < len(lines) else total
    return start, end


def _top_nodes(text: str) -> tuple[dict[str, ast.AST], list[str]]:
    tree = ast.parse(text)
    mapping: dict[str, ast.AST] = {}
    order: list[str] = []
    for node in tree.body:
        for name in _node_names(node):
            mapping[name] = node
            order.append(name)
    return mapping, order


def _segment(text: str, node: ast.AST) -> str:
    start, end = _span(text, node)
    return text[start:end].rstrip() + "\n"


def _digest_node(text: str, name: str) -> str:
    mapping, _ = _top_nodes(text)
    if name not in mapping:
        raise RuntimeError(f"required preserved node is missing: {name}")
    return hashlib.sha256(_segment(text, mapping[name]).encode("utf-8")).hexdigest()


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def _ensure_imports(text: str) -> str:
    if "import os\n" not in text:
        text = _replace_once(text, "import math\n", "import math\nimport os\n", label="os import")
    if "import threading\n" not in text:
        text = _replace_once(
            text,
            "import sqlite3\n",
            "import sqlite3\nimport threading\n",
            label="threading import",
        )
    if "from gateway import hosted_rooms\n" not in text:
        text = _replace_once(
            text,
            "from typing import Any, Callable, Iterator, Literal\n",
            "from typing import Any, Callable, Iterator, Literal\n\n"
            "from gateway import hosted_rooms\n",
            label="hosted_rooms import",
        )
    return text


def _remove_node(text: str, name: str) -> str:
    mapping, _ = _top_nodes(text)
    node = mapping.get(name)
    if node is None:
        return text
    start, end = _span(text, node)
    return text[:start] + text[end:].lstrip("\n")


def _replace_node(text: str, name: str, replacement: str) -> str:
    mapping, _ = _top_nodes(text)
    node = mapping.get(name)
    if node is None:
        raise RuntimeError(f"cannot replace absent node: {name}")
    start, end = _span(text, node)
    return text[:start] + replacement.rstrip() + "\n\n" + text[end:].lstrip("\n")


def _insert_missing_nodes(text: str, final_text: str) -> str:
    final_mapping, final_order = _top_nodes(final_text)
    for name in reversed(final_order):
        if name not in SELECTED_NODES:
            continue
        current_mapping, _ = _top_nodes(text)
        if name in current_mapping:
            continue
        index = final_order.index(name)
        anchor = next(
            (
                candidate
                for candidate in final_order[index + 1 :]
                if candidate in current_mapping
            ),
            None,
        )
        block = _segment(final_text, final_mapping[name]).rstrip() + "\n\n"
        if anchor is None:
            text = text.rstrip() + "\n\n" + block
        else:
            start, _ = _span(text, current_mapping[anchor])
            text = text[:start] + block + text[start:]
    return text


def merge_driver(current_text: str, final_text: str) -> tuple[str, dict[str, str]]:
    preserved_before = {
        name: _digest_node(current_text, name) for name in PRESERVED_NODES
    }
    text = _ensure_imports(current_text)
    for name in OBSOLETE_NODES:
        text = _remove_node(text, name)

    final_mapping, final_order = _top_nodes(final_text)
    for name in final_order:
        if name not in SELECTED_NODES:
            continue
        current_mapping, _ = _top_nodes(text)
        if name in current_mapping:
            text = _replace_node(text, name, _segment(final_text, final_mapping[name]))
    text = _insert_missing_nodes(text, final_text)
    ast.parse(text, filename=str(DRIVER_FILE))

    merged_mapping, _ = _top_nodes(text)
    missing = sorted(SELECTED_NODES - set(merged_mapping))
    if missing:
        raise RuntimeError(f"selected driver nodes are missing: {missing}")
    stale = sorted(OBSOLETE_NODES & set(merged_mapping))
    if stale:
        raise RuntimeError(f"obsolete driver nodes remain: {stale}")

    for name in SELECTED_NODES:
        if ast.dump(merged_mapping[name], include_attributes=False) != ast.dump(
            final_mapping[name], include_attributes=False
        ):
            raise RuntimeError(f"selected node differs from final source: {name}")

    preserved_after = {name: _digest_node(text, name) for name in PRESERVED_NODES}
    if preserved_after != preserved_before:
        raise RuntimeError(
            "candidate-only API changed during selective merge: "
            + json.dumps(
                {"before": preserved_before, "after": preserved_after},
                sort_keys=True,
            )
        )
    return text, preserved_after


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def write_focused_tests(final_test_text: str) -> None:
    if FOCUSED_TEST_FILE.exists():
        raise RuntimeError(f"focused test file already exists: {FOCUSED_TEST_FILE}")
    mapping, order = _top_nodes(final_test_text)
    required = set(TARGET_TESTS)
    changed = True
    while changed:
        changed = False
        for name in tuple(required):
            node = mapping.get(name)
            if node is None:
                raise RuntimeError(f"final test node is missing: {name}")
            for dependency in _loaded_names(node) & set(mapping):
                if dependency not in required:
                    required.add(dependency)
                    changed = True

    source = [
        '"""Focused schema and admission regressions preserved from PR #19."""',
        "",
        "from __future__ import annotations",
        "",
        "import sqlite3",
        "",
        "import pytest",
        "",
        "from gateway import hosted_room_driver as driver",
        "from gateway import hosted_rooms as rooms",
        "",
        "",
    ]
    for name in order:
        if name in required:
            source.append(_segment(final_test_text, mapping[name]).rstrip())
            source.extend(("", ""))
    rendered = "\n".join(source).rstrip() + "\n"
    ast.parse(rendered, filename=str(FOCUSED_TEST_FILE))
    FOCUSED_TEST_FILE.write_text(rendered, encoding="utf-8")


def patch_current_test_expectation() -> None:
    text = CURRENT_TEST_FILE.read_text(encoding="utf-8")
    old = 'with pytest.raises(driver.StaleTaskError, match="running task"):'
    new = 'with pytest.raises(driver.StaleTaskError, match="no longer running"):'
    text = _replace_once(text, old, new, label="stale approval expectation")
    ast.parse(text, filename=str(CURRENT_TEST_FILE))
    CURRENT_TEST_FILE.write_text(text, encoding="utf-8")


def action_write_tests(report_dir: Path) -> None:
    final_tests = git("show", f"{SOURCE_FINAL}:{CURRENT_TEST_FILE}").stdout
    write_focused_tests(final_tests)
    patch_current_test_expectation()
    (report_dir / "focused-tests.txt").write_text(
        "\n".join(TARGET_TESTS) + "\n", encoding="utf-8"
    )


def action_apply(report_dir: Path) -> None:
    current = DRIVER_FILE.read_text(encoding="utf-8")
    final = git("show", f"{SOURCE_FINAL}:{DRIVER_FILE}").stdout
    merged, preserved = merge_driver(current, final)
    DRIVER_FILE.write_text(merged, encoding="utf-8")
    git("add", str(DRIVER_FILE), str(CURRENT_TEST_FILE), str(FOCUSED_TEST_FILE))
    git("diff", "--cached", "--check")
    summary = {
        "source_final": SOURCE_FINAL,
        "selected_nodes": sorted(SELECTED_NODES),
        "preserved_nodes": preserved,
        "driver_lines": len(merged.splitlines()),
        "staged_diff_stat": git("diff", "--cached", "--stat").stdout.strip(),
    }
    (report_dir / "selective-merge-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (report_dir / "driver.diff").write_text(
        git("diff", "--cached", "--", str(DRIVER_FILE)).stdout,
        encoding="utf-8",
    )


def action_cleanup() -> None:
    for path in TEMPORARY_FILES:
        if path.exists():
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("write-tests", "apply", "cleanup"))
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    if args.action == "write-tests":
        action_write_tests(report_dir)
    elif args.action == "apply":
        action_apply(report_dir)
    else:
        action_cleanup()


if __name__ == "__main__":
    main()
