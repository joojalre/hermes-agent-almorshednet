from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path

PRODUCTION_FILE = Path("gateway/hosted_rooms.py")
SOURCE_BASE = "c19e98fbf5e0ceb243d066e3e46a4f304fb8d606"
SOURCE_FINAL = "20d0a6a42365b2b2351e1dca819022b6ec477b39"

RELEASE_MARKERS = (
    "from typing import Any, Iterator, Mapping, NoReturn",
    "def _legacy_members_match(",
    "def reserve_peer_room(",
    "def remote_run_receipt(",
    "def upsert_remote_run_receipt(",
    "def probe_peer_room_reservation(",
)
PATCH_MARKERS = (
    "_DISCUSSION_LIABILITY_PREFIX",
    "class RoomAdmissionBlockedError",
    "def _terminal_publication_liabilities(",
    "def _closing_discussion_liability_keys(",
    "def _is_terminal_recovery_plan(",
    "def _request_room_stop_locked(",
    "require_open_admissions: bool = False",
    "prospective_liability_keys",
    "released_liability_keys",
)
HELPER_FUNCTIONS = (
    "_stop_event_id",
    "_remaining_demotion_control_events",
    "_discussion_liability_key",
    "_discussion_source_state",
    "_pending_discussion_sources",
    "_terminal_event_matches_driver_task",
    "_published_terminal_task_outcomes",
    "_correlated_terminal_task_ids",
    "_terminal_task_discussion_liability_keys",
    "_closing_discussion_liability_keys",
    "_pending_discussion_liability_key_for_source",
    "_terminal_publication_liabilities",
    "_assert_terminal_recovery_headroom",
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


def _line_offsets(text: str) -> tuple[list[int], int]:
    offsets: list[int] = []
    total = 0
    for line in text.splitlines(keepends=True):
        offsets.append(total)
        total += len(line)
    return offsets, total


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


def _named_node(text: str, name: str) -> tuple[int, int, str]:
    tree = ast.parse(text)
    offsets, total = _line_offsets(text)
    lines = text.splitlines(keepends=True)
    for node in tree.body:
        if name not in _node_names(node):
            continue
        start = offsets[node.lineno - 1]
        end = offsets[node.end_lineno] if node.end_lineno < len(lines) else total
        return start, end, text[start:end]
    raise RuntimeError(f"top-level node not found: {name}")


def _source_node(source: str, name: str) -> str:
    return _named_node(source, name)[2].rstrip()


def _replace_node(text: str, name: str, replacement: str) -> str:
    start, end, _ = _named_node(text, name)
    return text[:start] + replacement.rstrip() + "\n\n" + text[end:].lstrip("\n")


def _insert_before(text: str, anchor: str, block: str) -> str:
    start, _, _ = _named_node(text, anchor)
    return text[:start] + block.rstrip() + "\n\n" + text[start:]


def _insert_after(text: str, anchor: str, block: str) -> str:
    _, end, _ = _named_node(text, anchor)
    return text[:end] + "\n" + block.rstrip() + "\n" + text[end:]


def _replace_capacity_header(text: str, source: str) -> str:
    start_marker = "CONTROL_EVENT_COUNT_RESERVE ="
    end_marker = "_JOURNAL_MODE_LOCK_RETRIES = 8"

    start = text.index(start_marker)
    end_start = text.index(end_marker, start)
    end = text.index("\n", end_start) + 1

    source_start = source.index(start_marker)
    source_end_start = source.index(end_marker, source_start)
    source_end = source.index("\n", source_end_start) + 1
    return text[:start] + source[source_start:source_end] + text[end:]


def _replace_once(value: str, old: str, new: str, *, label: str) -> str:
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return value.replace(old, new, 1)


def _patch_candidate_specific_capacity_calls(text: str) -> str:
    start, end, create_room = _named_node(text, "create_room")
    create_room = _replace_once(
        create_room,
        """                _assert_event_capacity(
                    conn,
                    room=existing,
""",
        """                _assert_event_capacity(
                    conn,
                    room_id=room_id,
                    room=existing,
""",
        label="legacy adoption capacity call",
    )
    text = text[:start] + create_room + text[end:]

    start, end, rename_room = _named_node(text, "rename_room")
    rename_room = _replace_once(
        rename_room,
        "_assert_event_capacity(conn, room=room, additional_bytes=event_bytes)",
        """_assert_event_capacity(
            conn,
            room_id=room_id,
            room=room,
            additional_bytes=event_bytes,
        )""",
        label="rename capacity call",
    )
    return text[:start] + rename_room + text[end:]


def _verify_capacity_calls(text: str) -> None:
    tree = ast.parse(text)
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_assert_event_capacity":
            continue
        if not any(keyword.arg == "room_id" for keyword in node.keywords):
            missing.append(node.lineno)
    if missing:
        raise RuntimeError(
            "capacity calls missing room_id at lines: "
            + ", ".join(str(line) for line in missing)
        )


def _verify_markers(text: str) -> None:
    for marker in RELEASE_MARKERS:
        if marker not in text:
            raise RuntimeError(f"release marker disappeared: {marker}")
    for marker in PATCH_MARKERS:
        if marker not in text:
            raise RuntimeError(f"storage invariant marker missing: {marker}")


def semantic_merge(candidate: str, source: str) -> str:
    text = _replace_capacity_header(candidate, source)

    if "_DISCUSSION_LIABILITY_PREFIX" not in text:
        text = _replace_once(
            text,
            "_JOURNAL_MODE_LOCK_RETRIES = 8\n",
            "_JOURNAL_MODE_LOCK_RETRIES = 8\n\n"
            + _source_node(source, "_DISCUSSION_LIABILITY_PREFIX")
            + "\n",
            label="discussion liability prefix anchor",
        )
    if "_FINAL_TERMINAL_EVENT_KINDS" not in text:
        text = _insert_before(
            text,
            "_ACTOR_FIELDS",
            _source_node(source, "_FINAL_TERMINAL_EVENT_KINDS"),
        )
    if "class RoomAdmissionBlockedError" not in text:
        text = _insert_before(
            text,
            "AuthoritySupersededError",
            _source_node(source, "RoomAdmissionBlockedError"),
        )

    helpers = "\n\n".join(_source_node(source, name) for name in HELPER_FUNCTIONS)
    text = _insert_before(text, "_assert_event_capacity", helpers)
    text = _replace_node(
        text,
        "_assert_event_capacity",
        _source_node(source, "_assert_event_capacity"),
    )
    text = _insert_after(
        text,
        "_assert_event_capacity",
        _source_node(source, "_is_terminal_recovery_plan"),
    )

    for name in (
        "append_event",
        "append_events",
        "claim_authority",
        "disband_room",
    ):
        text = _replace_node(text, name, _source_node(source, name))

    text = _insert_before(
        text,
        "request_room_stop",
        _source_node(source, "_request_room_stop_locked"),
    )
    text = _replace_node(
        text,
        "request_room_stop",
        _source_node(source, "request_room_stop"),
    )
    text = _patch_candidate_specific_capacity_calls(text)

    ast.parse(text, filename=str(PRODUCTION_FILE))
    _verify_capacity_calls(text)
    _verify_markers(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    status = git("status", "--porcelain").stdout.strip()
    if status != "?? tests/gateway/test_hosted_room_storage_reservations.py":
        raise RuntimeError(f"unexpected working tree before semantic merge: {status!r}")

    source_blob = git("rev-parse", f"{SOURCE_BASE}:{PRODUCTION_FILE}").stdout.strip()
    current_blob = git("hash-object", str(PRODUCTION_FILE)).stdout.strip()
    if current_blob != source_blob:
        raise RuntimeError(
            "candidate storage file moved before semantic merge: "
            f"expected {source_blob}, found {current_blob}"
        )

    candidate = PRODUCTION_FILE.read_text(encoding="utf-8")
    source = git("show", f"{SOURCE_FINAL}:{PRODUCTION_FILE}").stdout
    merged = semantic_merge(candidate, source)
    PRODUCTION_FILE.write_text(merged, encoding="utf-8")

    git("add", str(PRODUCTION_FILE))
    git("diff", "--cached", "--check")
    changed = [
        line
        for line in git("diff", "--cached", "--name-only").stdout.splitlines()
        if line
    ]
    if changed != [str(PRODUCTION_FILE)]:
        raise RuntimeError(f"unexpected staged paths: {changed}")

    summary = {
        "source_base": SOURCE_BASE,
        "source_final": SOURCE_FINAL,
        "source_blob": source_blob,
        "current_blob": current_blob,
        "merged_blob": git("hash-object", str(PRODUCTION_FILE)).stdout.strip(),
        "release_markers_preserved": list(RELEASE_MARKERS),
        "patch_markers_present": list(PATCH_MARKERS),
        "staged_diff_stat": git("diff", "--cached", "--stat").stdout.strip(),
    }
    (report_dir / "semantic-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (report_dir / "semantic-storage.diff").write_text(
        git("diff", "--cached", "--", str(PRODUCTION_FILE)).stdout,
        encoding="utf-8",
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
