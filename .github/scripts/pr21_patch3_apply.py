from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

SOURCE_FINAL = "20d0a6a42365b2b2351e1dca819022b6ec477b39"
PRODUCTION_FILE = Path("gateway/hosted_room_replicas.py")
FINAL_TEST_FILE = Path("tests/gateway/test_hosted_room_replicas.py")
FOCUSED_TEST_FILE = Path("tests/gateway/test_hosted_room_replicas_patch3.py")
TEMPORARY_FILES = (
    Path(".github/scripts/pr21_patch3_apply.py"),
    Path(".github/workflows/pr21-patch3-apply.yml"),
)

SELECTED_NODES = frozenset(
    {
        "ingest_page",
        "promote_replica",
        "validate_demotion_observation",
        "demote_room",
    }
)
REQUIRED_HOSTED_ROOM_IMPORTS = frozenset(
    {
        "MAX_ACTIVE_ROOMS",
        "_CRITICAL_CONTROL_EVENT_KINDS",
        "_assert_event_capacity",
        "_closing_discussion_liability_keys",
        "_correlated_terminal_task_ids",
        "_is_terminal_recovery_plan",
        "_terminal_publication_liabilities",
    }
)
TARGET_TESTS = (
    "test_ingest_preserves_monotonic_latest_seq_from_delayed_page",
    "test_promote_refuses_replica_that_has_not_caught_up",
    "test_promote_preflights_authoritative_event_count",
    "test_promote_preflights_authoritative_gateway_bytes",
    "test_promote_preflights_authoritative_room_bytes",
    "test_promote_replays_history_that_validly_used_stop_reserve",
    "test_promote_replays_correlated_terminal_pair_with_its_reserve",
    "test_promote_replays_closing_room_activity_with_discussion_reserve",
    "test_promote_refuses_when_active_room_capacity_is_full",
    "test_demote_fences_stale_local_authority",
    "test_demote_rejects_unpublished_terminal_liability_without_mutation",
    "test_demote_rejects_terminal_event_with_mismatched_task_correlation",
    "test_demote_preflights_authority_lost_control_capacity",
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
        start_line = min(start_line, *(int(item.lineno) for item in decorators))
    start = offsets[start_line - 1]
    end_line = int(node.end_lineno)
    end = offsets[end_line] if end_line < len(lines) else total
    return start, end


def _segment(text: str, node: ast.AST) -> str:
    start, end = _span(text, node)
    return text[start:end].rstrip() + "\n"


def _top_nodes(text: str) -> tuple[dict[str, ast.AST], list[str], ast.Module]:
    tree = ast.parse(text)
    mapping: dict[str, ast.AST] = {}
    order: list[str] = []
    for node in tree.body:
        for name in _node_names(node):
            mapping[name] = node
            order.append(name)
    return mapping, order, tree


def _digest_node(text: str, node: ast.AST) -> str:
    return hashlib.sha256(_segment(text, node).encode("utf-8")).hexdigest()


def _replace_node(text: str, name: str, replacement: str) -> str:
    mapping, _, _ = _top_nodes(text)
    node = mapping.get(name)
    if node is None:
        raise RuntimeError(f"cannot replace absent node: {name}")
    start, end = _span(text, node)
    return text[:start] + replacement.rstrip() + "\n\n" + text[end:].lstrip("\n")


def _replace_hosted_rooms_import(text: str) -> str:
    tree = ast.parse(text)
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "gateway.hosted_rooms"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one gateway.hosted_rooms import, found {len(matches)}"
        )
    node = matches[0]
    if any(alias.asname for alias in node.names):
        raise RuntimeError("aliased gateway.hosted_rooms imports are unsupported")
    names = sorted({alias.name for alias in node.names} | REQUIRED_HOSTED_ROOM_IMPORTS)
    replacement = "from gateway.hosted_rooms import (\n" + "".join(
        f"    {name},\n" for name in names
    ) + ")\n"
    start, end = _span(text, node)
    return text[:start] + replacement + text[end:].lstrip("\n")


def merge_production(current_text: str, final_text: str) -> tuple[str, dict[str, str]]:
    current_mapping, _, _ = _top_nodes(current_text)
    final_mapping, final_order, _ = _top_nodes(final_text)
    missing_current = sorted(SELECTED_NODES - set(current_mapping))
    missing_final = sorted(SELECTED_NODES - set(final_mapping))
    if missing_current or missing_final:
        raise RuntimeError(
            json.dumps(
                {"missing_current": missing_current, "missing_final": missing_final},
                sort_keys=True,
            )
        )

    preserved_before = {
        name: _digest_node(current_text, node)
        for name, node in current_mapping.items()
        if name not in SELECTED_NODES
    }

    text = _replace_hosted_rooms_import(current_text)
    for name in final_order:
        if name in SELECTED_NODES:
            text = _replace_node(text, name, _segment(final_text, final_mapping[name]))
    ast.parse(text, filename=str(PRODUCTION_FILE))

    merged_mapping, _, _ = _top_nodes(text)
    for name in SELECTED_NODES:
        if ast.dump(merged_mapping[name], include_attributes=False) != ast.dump(
            final_mapping[name], include_attributes=False
        ):
            raise RuntimeError(f"selected node differs from final PR19 source: {name}")

    preserved_after = {
        name: _digest_node(text, node)
        for name, node in merged_mapping.items()
        if name not in SELECTED_NODES
    }
    if preserved_after != preserved_before:
        changed = sorted(
            name
            for name in set(preserved_before) | set(preserved_after)
            if preserved_before.get(name) != preserved_after.get(name)
        )
        raise RuntimeError(f"unselected production nodes changed: {changed}")

    selected_digests = {
        name: _digest_node(text, merged_mapping[name]) for name in sorted(SELECTED_NODES)
    }
    return text, selected_digests


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def write_focused_tests(final_test_text: str) -> None:
    if FOCUSED_TEST_FILE.exists():
        raise RuntimeError(f"focused test file already exists: {FOCUSED_TEST_FILE}")
    mapping, order, tree = _top_nodes(final_test_text)
    required = set(TARGET_TESTS)
    changed = True
    while changed:
        changed = False
        for name in tuple(required):
            node = mapping.get(name)
            if node is None:
                raise RuntimeError(f"final PR19 test node is missing: {name}")
            for dependency in _loaded_names(node) & set(mapping):
                if dependency not in required:
                    required.add(dependency)
                    changed = True

    future_imports: list[str] = []
    other_imports: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        rendered = _segment(final_test_text, node).rstrip()
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future_imports.append(rendered)
        else:
            other_imports.append(rendered)

    blocks = [
        '"""Focused replica-correctness regressions preserved from PR #19."""',
        "",
        *future_imports,
        "",
        *other_imports,
        "",
        "",
    ]
    for name in order:
        if name in required:
            blocks.append(_segment(final_test_text, mapping[name]).rstrip())
            blocks.extend(("", ""))
    rendered = "\n".join(blocks).rstrip() + "\n"
    ast.parse(rendered, filename=str(FOCUSED_TEST_FILE))
    FOCUSED_TEST_FILE.write_text(rendered, encoding="utf-8")


def action_write_tests(report_dir: Path) -> None:
    final_tests = git("show", f"{SOURCE_FINAL}:{FINAL_TEST_FILE}").stdout
    write_focused_tests(final_tests)
    (report_dir / "focused-tests.txt").write_text(
        "\n".join(TARGET_TESTS) + "\n", encoding="utf-8"
    )


def action_apply(report_dir: Path) -> None:
    current = PRODUCTION_FILE.read_text(encoding="utf-8")
    final = git("show", f"{SOURCE_FINAL}:{PRODUCTION_FILE}").stdout
    merged, selected_digests = merge_production(current, final)
    PRODUCTION_FILE.write_text(merged, encoding="utf-8")
    summary = {
        "source_final": SOURCE_FINAL,
        "selected_nodes": sorted(SELECTED_NODES),
        "selected_digests": selected_digests,
        "production_lines": len(merged.splitlines()),
    }
    (report_dir / "selective-merge-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
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
