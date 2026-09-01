from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

STORAGE_TESTS = Path("tests/gateway/test_hosted_rooms.py")
TEST_NAME = "test_room_log_pages_are_bounded_by_serialized_event_bytes"


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


REPLACEMENT = '''def test_room_log_pages_are_bounded_by_serialized_event_bytes(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _create(db)
    for index in range(4):
        _append(
            db,
            room_id="room-1",
            event_id=f"message-{index}",
            kind="message.user",
            actor=USER,
            payload={"text": "x" * 180, "index": index},
        )

    def page_bytes(page):
        return len(
            json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )

    # created_at is serialized into every event and its float representation
    # can differ by a byte or more between adjacent events. Size every possible
    # single-event page instead of assuming the first event is the largest.
    single_event_pages = [
        rooms.read_events(
            db,
            room_id="room-1",
            since_seq=since_seq,
            limit=1,
        )
        for since_seq in range(4)
    ]
    budget = max(page_bytes(page) for page in single_event_pages) + 1
    assert page_bytes(rooms.read_events(db, room_id="room-1", limit=2)) > budget
    monkeypatch.setattr(rooms, "MAX_LOG_PAGE_BYTES", budget)

    first = rooms.read_events(db, room_id="room-1", limit=4)
    assert len(first["events"]) == 1
    assert first["has_more"] is True
    second = rooms.read_events(
        db,
        room_id="room-1",
        since_seq=first["cursor"],
        limit=4,
    )
    assert second["events"][0]["seq"] == first["cursor"] + 1
'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    args = parser.parse_args()

    text = STORAGE_TESTS.read_text(encoding="utf-8")
    node = _top_function(text, TEST_NAME)
    start, end = _span(text, node)
    merged = text[:start] + REPLACEMENT.rstrip() + "\n\n" + text[end:].lstrip("\n")
    ast.parse(merged, filename=str(STORAGE_TESTS))
    STORAGE_TESTS.write_text(merged, encoding="utf-8")

    required = (
        "single_event_pages = [",
        "budget = max(page_bytes(page) for page in single_event_pages) + 1",
        "created_at is serialized into every event",
    )
    for marker in required:
        if marker not in merged:
            raise RuntimeError(f"missing deterministic replay-page marker: {marker}")

    summary = {
        "test": TEST_NAME,
        "change": "size every single-event page before setting the replay-byte budget",
        "production_files_changed": [],
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
