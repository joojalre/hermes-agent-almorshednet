from __future__ import annotations

import argparse
import ast
import subprocess
from pathlib import Path

SOURCE_BASE = "c19e98fbf5e0ceb243d066e3e46a4f304fb8d606"
SOURCE_FINAL = "20d0a6a42365b2b2351e1dca819022b6ec477b39"
PRODUCTION_FILE = Path("gateway/hosted_rooms.py")
TEST_FILE = Path("tests/gateway/test_hosted_room_storage_reservations.py")
TEMPORARY_FILES = (
    Path(".github/scripts/pr21_patch1_apply.py"),
    Path(".github/workflows/pr21-patch1-apply.yml"),
    Path(".github/workflows/pr21-patch1-probe.yml"),
)

TEST_SOURCE = r'''"""Focused storage regressions preserved from the final PR #19 state."""

from __future__ import annotations

import inspect
import json

import pytest

from gateway import hosted_rooms as rooms


USER = {"kind": "user", "id": "desktop-user", "display_name": "User"}
GATEWAY_A = {"kind": "gateway", "id": "gateway-a"}


def _create(db):
    return rooms.create_room(
        db,
        room_id="room-1",
        name="Release room",
        members=[{"profile": "ops", "handle": "ops"}],
        authority_gateway_id="gateway-a",
        now=10,
    )


def _append(db, **kwargs):
    if kwargs.get("kind") == "message.user":
        kwargs.setdefault("authority_gateway_id", "gateway-a")
        kwargs.setdefault("authority_epoch", 1)
    return rooms.append_event(db, **kwargs)


_TERMINAL_CORRELATION_FIELDS = (
    "task_id",
    "discussion_event_id",
    "member_id",
    "thread_id",
    "turn_id",
)


def _valid_terminal_correlation():
    return {
        "task_id": "task-1",
        "discussion_event_id": "discussion-1",
        "member_id": "member-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
    }


def _terminal_recovery_events(state, *, suffix, member_payload, terminal_payload):
    member_event_id = f"member-{suffix}"
    return [
        {
            "room_id": "room-1",
            "event_id": member_event_id,
            "kind": "message.member",
            "actor": {"kind": "member", "id": "ops"},
            "payload": member_payload,
            "authority_gateway_id": state["authority_gateway_id"],
            "authority_epoch": state["authority_epoch"],
        },
        {
            "room_id": "room-1",
            "event_id": f"terminal-{suffix}",
            "kind": "turn.settled",
            "actor": GATEWAY_A,
            "payload": {
                "message_event_id": member_event_id,
                "passed": False,
                **terminal_payload,
            },
            "authority_gateway_id": state["authority_gateway_id"],
            "authority_epoch": state["authority_epoch"],
        },
    ]


def _terminal_recovery_capacity_fixture(db, monkeypatch):
    _create(db)
    _append(
        db,
        room_id="room-1",
        event_id="message-1",
        kind="message.user",
        actor=USER,
        payload={"text": "first"},
    )
    monkeypatch.setattr(rooms, "MAX_EVENTS_PER_ROOM", 1)
    monkeypatch.setattr(rooms, "STOP_EVENT_COUNT_RESERVE", 0)
    monkeypatch.setattr(rooms, "TERMINAL_RECOVERY_COUNT_RESERVE", 2)
    return rooms.room_state(db, room_id="room-1")


def test_terminal_recovery_plan_accepts_complete_correlation(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    state = _terminal_recovery_capacity_fixture(db, monkeypatch)
    correlation = _valid_terminal_correlation()

    published = rooms.append_events(
        db,
        events=_terminal_recovery_events(
            state,
            suffix="valid",
            member_payload=correlation,
            terminal_payload=correlation,
        ),
        allow_terminal_recovery=True,
    )

    assert [event["kind"] for event in published] == [
        "message.member",
        "turn.settled",
    ]


def test_malformed_terminal_correlation_does_not_consume_reserve(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    state = _terminal_recovery_capacity_fixture(db, monkeypatch)
    member_payload = _valid_terminal_correlation()
    terminal_payload = {
        **_valid_terminal_correlation(),
        "turn_id": "different-turn",
    }

    with pytest.raises(rooms.HostedRoomError, match="history limit"):
        rooms.append_events(
            db,
            events=_terminal_recovery_events(
                state,
                suffix="invalid",
                member_payload=member_payload,
                terminal_payload=terminal_payload,
            ),
            allow_terminal_recovery=True,
        )
    assert rooms.room_state(db, room_id="room-1")["latest_seq"] == 1

    correlation = _valid_terminal_correlation()
    published = rooms.append_events(
        db,
        events=_terminal_recovery_events(
            state,
            suffix="valid-after-rejection",
            member_payload=correlation,
            terminal_payload=correlation,
        ),
        allow_terminal_recovery=True,
    )
    assert len(published) == 2


def _require_open_admission_support():
    assert "require_open_admissions" in inspect.signature(
        rooms.append_event
    ).parameters


def test_open_admission_reservation_is_durable_across_transactions(
    tmp_path,
    monkeypatch,
):
    _require_open_admission_support()
    db = tmp_path / "state.db"
    _create(db)
    monkeypatch.setattr(rooms, "TERMINAL_RECOVERY_COUNT_RESERVE", 2)

    _append(
        db,
        room_id="room-1",
        event_id="discussion-1",
        kind="message.user",
        actor=USER,
        payload={"text": "first", "thread_id": "thread-1"},
        require_open_admissions=True,
    )
    _append(
        db,
        room_id="room-1",
        event_id="discussion-1-follow-up",
        kind="message.user",
        actor=USER,
        payload={"text": "replace", "thread_id": "thread-1"},
        require_open_admissions=True,
    )

    with rooms._connect(db) as conn:
        assert rooms._terminal_publication_liabilities(conn) == {
            rooms._discussion_liability_key("room-1", "thread-1")
        }

    _append(
        db,
        room_id="room-1",
        event_id="unrelated-terminal",
        kind="turn.cancelled",
        actor=GATEWAY_A,
        payload={
            "task_id": "unrelated-task",
            "discussion_event_id": "discussion-1-follow-up",
        },
        authority_gateway_id="gateway-a",
        authority_epoch=1,
    )
    with rooms._connect(db) as conn:
        assert rooms._terminal_publication_liabilities(conn) == {
            rooms._discussion_liability_key("room-1", "thread-1")
        }

    with pytest.raises(rooms.HostedRoomError, match="unpublished terminal work"):
        _append(
            db,
            room_id="room-1",
            event_id="discussion-2",
            kind="message.user",
            actor=USER,
            payload={"text": "second", "thread_id": "thread-2"},
            require_open_admissions=True,
        )


def test_room_activity_releases_only_matching_discussion_liability(
    tmp_path,
    monkeypatch,
):
    _require_open_admission_support()
    db = tmp_path / "state.db"
    _create(db)
    monkeypatch.setattr(rooms, "TERMINAL_RECOVERY_COUNT_RESERVE", 2)

    _append(
        db,
        room_id="room-1",
        event_id="discussion-1",
        kind="message.user",
        actor=USER,
        payload={"text": "first", "thread_id": "thread-1"},
        require_open_admissions=True,
    )
    _append(
        db,
        room_id="room-1",
        event_id="activity-wrong-thread",
        kind="room.activity",
        actor=GATEWAY_A,
        payload={
            "status": "settled",
            "discussion_event_id": "discussion-1",
            "thread_id": "thread-other",
        },
        authority_gateway_id="gateway-a",
        authority_epoch=1,
    )
    with pytest.raises(rooms.HostedRoomError, match="unpublished terminal work"):
        _append(
            db,
            room_id="room-1",
            event_id="discussion-2-before-close",
            kind="message.user",
            actor=USER,
            payload={"text": "second", "thread_id": "thread-2"},
            require_open_admissions=True,
        )

    _append(
        db,
        room_id="room-1",
        event_id="activity-correct-thread",
        kind="room.activity",
        actor=GATEWAY_A,
        payload={
            "status": "settled",
            "discussion_event_id": "discussion-1",
            "thread_id": "thread-1",
        },
        authority_gateway_id="gateway-a",
        authority_epoch=1,
    )
    accepted = _append(
        db,
        room_id="room-1",
        event_id="discussion-2-after-close",
        kind="message.user",
        actor=USER,
        payload={"text": "second", "thread_id": "thread-2"},
        require_open_admissions=True,
    )
    assert accepted["event_id"] == "discussion-2-after-close"


def test_terminal_recovery_plan_rejects_non_object_payloads():
    pending = [
        (
            0,
            {
                "event_id": "member-1",
                "kind": "message.member",
                "payload_json": json.dumps([]),
            },
        ),
        (
            1,
            {
                "event_id": "terminal-1",
                "kind": "turn.settled",
                "payload_json": json.dumps({}),
            },
        ),
    ]

    assert rooms._is_terminal_recovery_plan(pending) is False
'''


def run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode:
        raise RuntimeError(
            f"command failed ({process.returncode}): {' '.join(args)}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return process


def write_tests() -> None:
    if TEST_FILE.exists():
        raise RuntimeError(f"focused test file already exists: {TEST_FILE}")
    ast.parse(TEST_SOURCE, filename=str(TEST_FILE))
    TEST_FILE.write_text(TEST_SOURCE, encoding="utf-8")


def apply_production(report_dir: Path) -> None:
    patch = run(
        "git",
        "diff",
        "--binary",
        SOURCE_BASE,
        SOURCE_FINAL,
        "--",
        str(PRODUCTION_FILE),
    ).stdout
    if not patch.strip():
        raise RuntimeError("storage delta is empty")
    patch_path = report_dir / "storage.patch"
    patch_path.write_text(patch, encoding="utf-8")
    run("git", "apply", "--3way", "--index", str(patch_path))
    merged = PRODUCTION_FILE.read_text(encoding="utf-8")
    ast.parse(merged, filename=str(PRODUCTION_FILE))
    required_markers = (
        "_DISCUSSION_LIABILITY_PREFIX",
        "def _terminal_publication_liabilities(",
        "def _closing_discussion_liability_keys(",
        "def _is_terminal_recovery_plan(",
        "require_open_admissions: bool = False",
        "prospective_liability_keys",
        "released_liability_keys",
    )
    for marker in required_markers:
        if marker not in merged:
            raise RuntimeError(f"missing storage invariant marker: {marker}")


def cleanup() -> None:
    for path in TEMPORARY_FILES:
        if path.exists():
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("write-tests", "apply-production", "cleanup"))
    parser.add_argument("--report-dir")
    args = parser.parse_args()

    if args.action == "write-tests":
        write_tests()
    elif args.action == "apply-production":
        if not args.report_dir:
            raise RuntimeError("--report-dir is required")
        report_dir = Path(args.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        apply_production(report_dir)
    else:
        cleanup()


if __name__ == "__main__":
    main()
