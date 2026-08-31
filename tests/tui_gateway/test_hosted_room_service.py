"""Integration tests for the hosted Discussion coordinator."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms
from gateway.hosted_room_policy_checkpoint import MAX_ACTIVE_POLICY_EVENTS
from tui_gateway.hosted_room_service import HostedRoomService


def _append_room_event(db, **kwargs):
    if kwargs.get("kind") == "message.user":
        room = hosted_rooms.room_state(db, room_id=kwargs["room_id"])
        kwargs.setdefault(
            "authority_gateway_id", str(room["authority_gateway_id"])
        )
        kwargs.setdefault("authority_epoch", int(room["authority_epoch"]))
    return hosted_rooms.append_event(db, **kwargs)


class _FakeRPC:
    def __init__(self) -> None:
        self.sessions = {}

    def resolve_exact(self, *, profile, title, source):
        return self.sessions.get((profile, title))

    def create(self, *, profile, title, source):
        session = {"session_id": f"{profile}-session", "title": title}
        self.sessions[(profile, title)] = session
        return session

    def resume(self, *, profile, session_id, source):
        return {"session_id": session_id}

    def submit(
        self,
        *,
        profile,
        session_id,
        prompt,
        source,
        task,
        execution_generation,
        on_terminal,
    ):
        on_terminal({"status": "settled", "text": f"reply from {profile}"})
        return {"accepted": True}

    def history(self, *, profile, session_id, source):
        return []

    def info(self, *, profile, session_id, source):
        return {"active": False, "task_id": None}

    def interrupt_admitted(self, *, task, execution_generation, source):
        return {"found": False, "active": False, "interrupted": False}


class _PromptRecordingRPC(_FakeRPC):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[tuple[str, str]] = []

    def submit(
        self,
        *,
        profile,
        session_id,
        prompt,
        source,
        task,
        execution_generation,
        on_terminal,
    ):
        self.prompts.append((profile, prompt))
        on_terminal({"status": "settled", "text": f"reply from {profile}"})
        return {"accepted": True}


class _ProfileRecordingRPC(_PromptRecordingRPC):
    def __init__(self) -> None:
        super().__init__()
        self.profile_calls: list[tuple[str, str]] = []

    def resolve_exact(self, *, profile, title, source):
        self.profile_calls.append(("resolve_exact", profile))
        return super().resolve_exact(profile=profile, title=title, source=source)

    def create(self, *, profile, title, source):
        self.profile_calls.append(("create", profile))
        return super().create(profile=profile, title=title, source=source)

    def resume(self, *, profile, session_id, source):
        self.profile_calls.append(("resume", profile))
        return super().resume(profile=profile, session_id=session_id, source=source)

    def submit(self, **kwargs):
        self.profile_calls.append(("submit", kwargs["profile"]))
        return super().submit(**kwargs)


class _BlockingFirstRPC(_PromptRecordingRPC):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def submit(self, **kwargs):
        self.prompts.append((kwargs["profile"], kwargs["prompt"]))
        if len(self.prompts) == 1:
            self.first_started.set()
            assert self.release_first.wait(timeout=2)
        kwargs["on_terminal"](
            {"status": "settled", "text": f"reply from {kwargs['profile']}"}
        )
        return {"accepted": True}


class _InterruptibleRPC(_FakeRPC):
    def __init__(self, *, acknowledge_interrupt: bool = True) -> None:
        super().__init__()
        self.acknowledge_interrupt = acknowledge_interrupt
        self.started = threading.Event()
        self.interrupted = threading.Event()
        self.active_task_id: str | None = None
        self.active_task = None
        self.execution_generation = None

    def submit(self, **kwargs):
        self.active_task_id = kwargs["task"].task_id
        self.active_task = kwargs["task"]
        self.execution_generation = kwargs["execution_generation"]
        self.started.set()
        return {"accepted": True}

    def info(self, *, profile, session_id, source):
        return {
            "active": self.active_task_id is not None,
            "task_id": self.active_task_id,
        }

    def interrupt_admitted(self, *, task, execution_generation, source):
        if (
            self.active_task != task
            or self.execution_generation != execution_generation
        ):
            return {"found": False, "active": False, "interrupted": False}
        if not self.acknowledge_interrupt:
            return {"found": True, "active": True, "interrupted": False}
        self.active_task_id = None
        self.active_task = None
        self.interrupted.set()
        return {"found": True, "active": True, "interrupted": True}


def _server():
    return SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def _approve_room_task_process(
    db_path: str,
    room_id: str,
    member_id: str,
    task_id: str,
    execution_generation: int,
    request_id: str,
    choice: str,
    results,
) -> None:
    """Exercise the dashboard decision through a distinct spawned process."""

    try:
        service = HostedRoomService(_server(), db_path=Path(db_path))
        result = service.approve_room_task(
            room_id,
            member_id=member_id,
            task_id=task_id,
            execution_generation=execution_generation,
            request_id=request_id,
            choice=choice,
        )
    except Exception as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})
    else:
        results.put({"result": dict(result)})


def test_create_send_drive_publish_and_replay_without_client_transport(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    assert room["room_id"] == "room-1"

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect the release", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member" for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    assert [event["kind"] for event in events][:3] == [
        "message.user",
        "message.member",
        "turn.settled",
    ]
    assert events[1]["payload"]["text"] == "reply from ops"
    assert service.status("room-1")["working"] is False


def test_profile_deleted_after_planning_is_deferred_before_admission(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    profile_snapshots = iter(
        (("default", "ops"), ("default", "ops"), ("default",))
    )
    service.local_profiles = lambda: next(profile_snapshots, ("default",))

    service.prepare_room(service.bindings()[0])

    assert driver.list_tasks(db, room_id="room-1") == []
    deferred = next(
        event
        for event in service._events("room-1")
        if event["kind"] == "turn.deferred"
    )
    assert deferred["payload"]["reason"] == "member_unavailable"
    assert rpc.sessions == {}


def test_deleted_frozen_member_is_deferred_before_session_resolution(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _ProfileRecordingRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("research", "ops")
    service.create_room(
        room_id="room-1",
        name="Frozen roster",
        members=[
            {
                "member_id": "research",
                "profile": "research",
                "handle": "research",
            },
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )

    # The persisted roster remains valid, but the deleted profile must never
    # reach session resolution where it could fall back to the launch profile.
    service.local_profiles = lambda: ("default", "ops")
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "Report.", "thread_id": "thread-1"},
    )
    for _ in range(3):
        service.runtime._run_cycle()

    events = service._events("room-1")
    deferred = next(event for event in events if event["kind"] == "turn.deferred")
    assert deferred["payload"]["member_id"] == "research"
    assert deferred["payload"]["reason"] == "member_unavailable"
    assert rpc.profile_calls == [
        ("resolve_exact", "ops"),
        ("create", "ops"),
        ("submit", "ops"),
    ]
    assert [profile for profile, _prompt in rpc.prompts] == ["ops"]
    assert any(
        event["kind"] == "message.member"
        and event["payload"]["member_id"] == "ops"
        for event in events
    )
    assert {
        task["payload"]["target_profile"]
        for task in driver.list_tasks(db, room_id="room-1")
    } == {"ops"}


def test_profile_deleted_after_admission_is_deferred_and_peer_continues(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _ProfileRecordingRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("research", "ops")
    service.create_room(
        room_id="room-1",
        name="Runtime profile fence",
        members=[
            {
                "member_id": "research",
                "profile": "research",
                "handle": "research",
            },
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "Report.", "thread_id": "thread-1"},
    )
    assert {
        task["payload"]["target_profile"]
        for task in driver.list_tasks(db, room_id="room-1")
    } == {"research"}

    service.local_profiles = lambda: ("default", "ops")
    for _ in range(3):
        service.runtime._run_cycle()

    tasks = {
        task["payload"]["target_profile"]: task
        for task in driver.list_tasks(db, room_id="room-1")
    }
    assert tasks["research"]["status"] == "deferred"
    assert tasks["research"]["result"] == {
        "reason": "member_unavailable",
        "retryable": True,
    }
    assert tasks["ops"]["status"] == "settled"
    assert rpc.profile_calls == [
        ("resolve_exact", "ops"),
        ("create", "ops"),
        ("submit", "ops"),
    ]
    events = service._events("room-1")
    assert any(
        event["kind"] == "turn.deferred"
        and event["payload"]["member_id"] == "research"
        and event["payload"]["reason"] == "member_unavailable"
        for event in events
    )
    assert any(
        event["kind"] == "message.member"
        and event["payload"]["member_id"] == "ops"
        for event in events
    )


def test_demotion_interrupts_inflight_turn_before_authority_changes(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _InterruptibleRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )

    service.start()
    try:
        service.send(
            room_id="room-1",
            event_id="user-1",
            payload={"text": "@ops inspect", "thread_id": "thread-1"},
        )
        assert rpc.started.wait(timeout=1.0)

        observed_gateway = "install:" + "b" * 32
        result = service.demote_room(
            "room-1",
            observed_gateway_id=observed_gateway,
            observed_epoch=2,
        )

        assert rpc.interrupted.is_set()
        assert result["authority_gateway_id"] == observed_gateway
        assert result["authority_epoch"] == 2
        tasks = driver.list_tasks(db, room_id="room-1")
        assert [task["status"] for task in tasks] == ["cancelled"]
    finally:
        service.stop(timeout=1.0)


def test_demotion_keeps_local_authority_when_interrupt_is_not_acknowledged(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _InterruptibleRPC(acknowledge_interrupt=False)
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )

    service.start()
    try:
        service.send(
            room_id="room-1",
            event_id="user-1",
            payload={"text": "@ops inspect", "thread_id": "thread-1"},
        )
        assert rpc.started.wait(timeout=1.0)
        original = hosted_rooms.room_state(db, room_id="room-1")

        with pytest.raises(RuntimeError, match="still stopping"):
            service.demote_room(
                "room-1",
                observed_gateway_id="install:" + "b" * 32,
                observed_epoch=2,
            )

        current = hosted_rooms.room_state(db, room_id="room-1")
        assert current["authority_gateway_id"] == original["authority_gateway_id"]
        assert current["authority_epoch"] == original["authority_epoch"]
        assert "authority.lost" not in {
            event["kind"]
            for event in hosted_rooms.read_events(
                db, room_id="room-1", since_seq=0, limit=100
            )["events"]
        }
    finally:
        service.stop(timeout=1.0)


def test_restart_republishes_terminal_task_before_admitting_more(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    event = _append_room_event(
        db,
        room_id="room-1",
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    binding = service.bindings()[0]
    service.prepare_room(binding)
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="crashed",
        ttl_seconds=30,
        clock=time.time,
    )
    attempt = driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    driver.settle_task(
        db,
        attempt,
        settlement_id="reply-1",
        status="settled",
        result={"text": "done"},
        clock=time.time,
    )

    service.prepare_room(binding)
    events = service._events("room-1")
    assert event["seq"] == 1
    assert sum(row["kind"] == "message.member" for row in events) == 1
    assert sum(row["kind"] == "turn.settled" for row in events) == 1
    service.prepare_room(binding)
    replayed = service._events("room-1")
    assert replayed == events


def test_terminal_publication_retries_after_a_newer_user_wins_the_append_race(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops first", "thread_id": "thread-1"},
    )
    binding = service.bindings()[0]
    service.prepare_room(binding)
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="worker",
        ttl_seconds=30,
        clock=time.time,
    )
    attempt = driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    driver.settle_task(
        db,
        attempt,
        settlement_id="late-reply",
        status="settled",
        result={"text": "stale answer"},
        clock=time.time,
    )

    original_append_events = hosted_rooms.append_events
    injected = False

    def append_after_newer_user(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            _append_room_event(
                db,
                room_id="room-1",
                event_id="user-2",
                kind="message.user",
                actor={"kind": "user", "id": "desktop"},
                payload={"text": "@ops newer", "thread_id": "thread-1"},
            )
        return original_append_events(*args, **kwargs)

    monkeypatch.setattr(hosted_rooms, "append_events", append_after_newer_user)
    with pytest.raises(hosted_rooms.RoomConflictError, match="latest sequence"):
        service.prepare_room(binding)

    assert not any(
        event["kind"].startswith("turn.") and event["payload"].get("task_id") == task["identity"].task_id
        for event in service._events("room-1")
    )
    service.prepare_room(binding)
    terminal = next(
        event
        for event in service._events("room-1")
        if event["kind"] == "turn.cancelled"
        and event["payload"].get("task_id") == task["identity"].task_id
    )
    assert terminal["payload"]["reason"] == "superseded_by_newer_user_event"


def test_terminal_publication_reserves_the_whole_plan_before_append(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    monkeypatch.setattr(hosted_rooms, "MAX_EVENTS_PER_ROOM", 2)
    plan = discussion.PublicationPlan(
        task_id="task-1",
        terminal_kind="turn.settled",
        events=(
            discussion.EventPlan(
                event_id="member-1",
                kind="message.member",
                actor={"kind": "member", "id": "ops", "profile": "ops"},
                payload={
                    "discussion_event_id": "user-1",
                    "member_id": "ops",
                    "member_index": 1,
                    "round_index": 0,
                    "task_id": "task-1",
                    "text": "done",
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                },
                authority_gateway_id=str(room["authority_gateway_id"]),
                authority_epoch=int(room["authority_epoch"]),
            ),
            discussion.EventPlan(
                event_id="terminal-1",
                kind="turn.settled",
                actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
                payload={
                    "discussion_event_id": "user-1",
                    "member_id": "ops",
                    "member_index": 1,
                    "message_event_id": "member-1",
                    "passed": False,
                    "round_index": 0,
                    "seen_through_seq": 1,
                    "task_id": "task-1",
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                },
                authority_gateway_id=str(room["authority_gateway_id"]),
                authority_epoch=int(room["authority_epoch"]),
            ),
        ),
    )

    appended = service._append_plan("room-1", plan)

    assert [event["event_id"] for event in service._events("room-1")] == [
        "user-1",
        "member-1",
        "terminal-1",
    ]
    assert [event["event_id"] for event in appended] == [
        "member-1",
        "terminal-1",
    ]
    with pytest.raises(hosted_rooms.HostedRoomError, match="history limit"):
        hosted_rooms.append_events(
            db,
            events=[
                {
                    "room_id": "room-1",
                    "event_id": "ordinary-1",
                    "kind": "message.user",
                    "actor": {"kind": "user", "id": "desktop"},
                    "payload": {"text": "ordinary"},
                    "authority_gateway_id": str(room["authority_gateway_id"]),
                    "authority_epoch": int(room["authority_epoch"]),
                }
            ],
            allow_terminal_recovery=True,
        )


def test_terminal_publication_recovers_correlated_member_prefix_at_normal_limit(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    member = discussion.EventPlan(
        event_id="member-1",
        kind="message.member",
        actor={"kind": "member", "id": "ops"},
        payload={
            "text": "done",
            "task_id": "task-1",
            "discussion_event_id": "discussion-1",
            "member_id": "ops",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        },
        authority_gateway_id=str(room["authority_gateway_id"]),
        authority_epoch=int(room["authority_epoch"]),
    )
    terminal = discussion.EventPlan(
        event_id="terminal-1",
        kind="turn.settled",
        actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
        payload={
            "task_id": "task-1",
            "discussion_event_id": "discussion-1",
            "member_id": "ops",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "message_event_id": "member-1",
            "passed": False,
        },
        authority_gateway_id=str(room["authority_gateway_id"]),
        authority_epoch=int(room["authority_epoch"]),
    )
    hosted_rooms.append_event(db, **member.append_kwargs("room-1"))
    monkeypatch.setattr(hosted_rooms, "MAX_EVENTS_PER_ROOM", 1)

    service._append_plan(
        "room-1",
        discussion.PublicationPlan(
            task_id="task-1",
            terminal_kind="turn.settled",
            events=(member, terminal),
        ),
    )
    assert [event["event_id"] for event in service._events("room-1")] == [
        "member-1",
        "terminal-1",
    ]


def test_terminal_only_publication_uses_bounded_recovery_reserve(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    monkeypatch.setattr(hosted_rooms, "MAX_EVENTS_PER_ROOM", 1)
    terminal = discussion.EventPlan(
        event_id="cancelled-1",
        kind="turn.cancelled",
        actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
        payload={"task_id": "task-1", "reason": "stopped"},
        authority_gateway_id=str(room["authority_gateway_id"]),
        authority_epoch=int(room["authority_epoch"]),
    )

    service._append_plan(
        "room-1",
        discussion.PublicationPlan(
            task_id="task-1",
            terminal_kind="turn.cancelled",
            events=(terminal,),
        ),
    )
    assert [event["event_id"] for event in service._events("room-1")] == [
        "user-1",
        "cancelled-1",
    ]


def test_room_activity_refuses_stale_snapshot_after_same_thread_followup(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Racing room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-old",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "first", "thread_id": "thread-1"},
    )
    stale_room = hosted_rooms.room_state(db, room_id="room-1")
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-new",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "follow up", "thread_id": "thread-1"},
    )
    stale_decision = discussion.DiscussionDecision(
        status="settled",
        reason="silent_round",
        discussion_event_id="user-old",
        source_event_seq=1,
        thread_id="thread-1",
    )

    with pytest.raises(hosted_rooms.HostedRoomError, match="changed"):
        service._append_room_status(stale_room, stale_decision)

    fresh_room = hosted_rooms.room_state(db, room_id="room-1")
    hosted_rooms.append_event(
        db,
        room_id="room-1",
        event_id="legacy-stale-activity",
        kind="room.activity",
        actor={"kind": "gateway", "id": str(fresh_room["authority_gateway_id"])},
        payload={
            "status": "settled",
            "reason_code": "silent_round",
            "thread_id": "thread-1",
            "discussion_event_id": "user-old",
        },
        authority_gateway_id=str(fresh_room["authority_gateway_id"]),
        authority_epoch=int(fresh_room["authority_epoch"]),
    )
    fresh_room = hosted_rooms.room_state(db, room_id="room-1")
    snapshot = service._policy_snapshot(fresh_room)
    assert any(event["event_id"] == "user-new" for event in snapshot.events)


def test_send_returns_durable_user_event_when_room_status_publication_races(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Racing room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    monkeypatch.setattr(
        discussion,
        "plan_next_task",
        lambda *args, **kwargs: discussion.DiscussionDecision(
            status="settled",
            reason="silent_round",
            discussion_event_id="user-1",
            source_event_seq=1,
            thread_id="thread-1",
        ),
    )
    original_append_events = hosted_rooms.append_events
    injected = False

    def append_after_newer_user(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            _append_room_event(
                db,
                room_id="room-1",
                event_id="user-2",
                kind="message.user",
                actor={"kind": "user", "id": "desktop"},
                payload={"text": "newer", "thread_id": "thread-1"},
            )
        return original_append_events(*args, **kwargs)

    monkeypatch.setattr(hosted_rooms, "append_events", append_after_newer_user)
    service.runtime._wake.clear()

    event = service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "first", "thread_id": "thread-1"},
    )

    assert event["event_id"] == "user-1"
    assert [event["event_id"] for event in service._events("room-1")] == [
        "user-1",
        "user-2",
    ]
    assert service.runtime._wake.is_set()

def test_terminal_publication_rejects_existing_suffix_without_member_prefix(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    member = discussion.EventPlan(
        event_id="member-1",
        kind="message.member",
        actor={"kind": "member", "id": "ops"},
        payload={"text": "done"},
        authority_gateway_id=str(room["authority_gateway_id"]),
        authority_epoch=int(room["authority_epoch"]),
    )
    terminal = discussion.EventPlan(
        event_id="terminal-1",
        kind="turn.settled",
        actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
        payload={"task_id": "task-1"},
        authority_gateway_id=str(room["authority_gateway_id"]),
        authority_epoch=int(room["authority_epoch"]),
    )
    hosted_rooms.append_event(db, **terminal.append_kwargs("room-1"))

    with pytest.raises(hosted_rooms.EventConflictError, match="ordered prefix"):
        service._append_plan(
            "room-1",
            discussion.PublicationPlan(
                task_id="task-1",
                terminal_kind="turn.settled",
                events=(member, terminal),
            ),
        )

    assert [event["event_id"] for event in service._events("room-1")] == [
        "terminal-1"
    ]


def test_policy_checkpoint_bounds_replay_after_completed_room_history(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Long-running room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "default"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    authority = str(room["authority_gateway_id"])
    rows = []
    for index in range(200):
        user_seq = index * 2 + 1
        activity_seq = user_seq + 1
        thread_id = f"thread-{index}"
        event_id = f"user-{index}"
        rows.extend((
            (
                "room-1",
                user_seq,
                event_id,
                "message.user",
                json.dumps({"kind": "user", "id": "load-test"}),
                None,
                json.dumps({"text": "done", "thread_id": thread_id}),
                float(user_seq),
            ),
            (
                "room-1",
                activity_seq,
                f"activity-{index}",
                "room.activity",
                json.dumps({"kind": "gateway", "id": authority}),
                1,
                json.dumps({
                    "status": "settled",
                    "reason_code": "silent_round",
                    "thread_id": thread_id,
                    "discussion_event_id": event_id,
                }),
                float(activity_seq),
            ),
        ))
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """INSERT INTO hosted_room_events(
                   room_id, seq, event_id, kind, actor_json,
                   authority_epoch, payload_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.execute(
            """UPDATE hosted_rooms
               SET next_seq=401, revision=revision+400, updated_at=400
               WHERE room_id='room-1'"""
        )
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-active",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "Review this", "thread_id": "thread-active"},
        now=401,
    )

    original_read_events = hosted_rooms.read_events
    reads = {"calls": 0, "rows": 0}

    def counted_read_events(*args, **kwargs):
        page = original_read_events(*args, **kwargs)
        reads["calls"] += 1
        reads["rows"] += len(page["events"])
        return page

    monkeypatch.setattr(hosted_rooms, "read_events", counted_read_events)
    binding = service.bindings()[0]
    service.prepare_room(binding)
    assert reads["rows"] == 401
    snapshot = service._policy_snapshot(hosted_rooms.room_state(db, room_id="room-1"))
    assert len(snapshot.events) == 1
    assert len(snapshot.events) <= MAX_ACTIVE_POLICY_EVENTS
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM hosted_room_policy_events").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM hosted_room_policy_threads").fetchone()[
                0
            ]
            == 1
        )

    reads.update(calls=0, rows=0)
    service.prepare_room(binding)
    assert reads == {"calls": 0, "rows": 0}


def test_same_thread_followup_migrates_and_delivers_committed_peer_reply(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _PromptRecordingRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Shared context room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops provide the marker", "thread_id": "thread-1"},
    )
    _wait_for(lambda: len(service.rpc.prompts) == 1)
    _wait_for(
        lambda: any(
            event["kind"] == "room.activity"
            and event["payload"]["discussion_event_id"] == "user-1"
            for event in service._events("room-1")
        )
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM hosted_room_policy_transcript
               WHERE room_id='room-1' AND thread_id='thread-1'"""
        ).fetchone()[0] == 2
        conn.execute("DELETE FROM hosted_room_policy_transcript")
        conn.execute(
            """DELETE FROM hosted_room_policy_transcript_state
               WHERE room_id='room-1'"""
        )
    service.send(
        room_id="room-1",
        event_id="user-2",
        payload={"text": "@hermes continue", "thread_id": "thread-1"},
    )
    _wait_for(lambda: len(service.rpc.prompts) == 2)
    assert service.stop(timeout=1.0)

    profile, prompt = service.rpc.prompts[1]
    assert profile == "default"
    assert "@ops: reply from ops" in prompt
    assert "User (user): @hermes continue" in prompt


def test_active_same_thread_followup_waits_for_current_task(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _BlockingFirstRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Serialized room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops start", "thread_id": "thread-1"},
    )
    assert service.rpc.first_started.wait(timeout=2)
    service.send(
        room_id="room-1",
        event_id="user-2",
        payload={"text": "@hermes follow up", "thread_id": "thread-1"},
    )
    assert len(service.rpc.prompts) == 1
    service.rpc.release_first.set()
    _wait_for(lambda: len(service.rpc.prompts) == 2)
    _wait_for(
        lambda: any(
            event["kind"] == "room.activity"
            and event["payload"]["discussion_event_id"] == "user-2"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)
    assert "User (user): @hermes follow up" in service.rpc.prompts[1][1]


def test_thread_transcript_prunes_committed_message_and_settlement_together(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Bounded room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.start()
    service.send(
        room_id="room-1",
        event_id="user-first",
        payload={"text": "@ops old", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "room.activity"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)
    for index in range(24):
        _append_room_event(
            db,
            room_id="room-1",
            event_id=f"user-tail-{index}",
            kind="message.user",
            actor={"kind": "user", "id": "desktop"},
            payload={"text": f"tail {index}", "thread_id": "thread-1"},
        )

    room = hosted_rooms.room_state(db, room_id="room-1")
    snapshot = service._policy_snapshot(room)
    assert len(snapshot.events) == 24
    assert {event["kind"] for event in snapshot.events} == {"message.user"}
    discussion.plan_next_task(
        room,
        snapshot.events,
        local_profiles=service.local_profiles(),
        initial_watermarks=snapshot.watermarks,
    )


def test_service_uses_low_idle_poll_with_immediate_wakeup(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("HERMES_AGENT_TIMEOUT", raising=False)
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")

    assert service.runtime.poll_interval_seconds == 5.0
    assert service.runtime.active_poll_interval_seconds == 0.25
    assert service.runtime.turn_timeout_seconds == 1830.0
    service.runtime._wake.clear()
    service.wakeup()
    assert service.runtime._wake.is_set()


def test_service_derives_room_deadline_from_agent_timeout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "90")

    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")

    assert service.runtime.turn_timeout_seconds == 120.0


def test_service_publishes_deferred_turn_and_retries_active_discussion(
    tmp_path: Path,
):
    now = [100.0]

    def clock():
        return now[0]

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.runtime.clock = clock
    service.runtime.lease_ttl_seconds = 30
    service.runtime.indeterminate_defer_seconds = 5
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Resilient room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "default"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-resilience",
        payload={"text": "Check this", "thread_id": "thread-1"},
    )
    first = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    old_lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=service.bindings()[0].gateway_id,
        authority_epoch=1,
        process_generation="offline-member",
        ttl_seconds=1,
        clock=clock,
    )
    old_attempt = driver.start_task(
        db,
        first["identity"],
        old_lease,
        expected_cancel_generation=0,
        clock=clock,
    )

    now[0] = 102.0
    binding = service.bindings()[0]
    service.runtime._process_room(binding)
    now[0] = 108.0
    indeterminate = driver.list_tasks(
        db,
        room_id="room-1",
        status="indeterminate",
    )[0]
    lease = service.runtime._leases["room-1"]
    driver.defer_indeterminate_task(
        db,
        first["identity"],
        lease,
        expected_execution_generation=indeterminate["execution_generation"],
        expected_cancel_generation=indeterminate["cancel_generation"],
        reason="member_unavailable",
        clock=clock,
    )
    room = hosted_rooms.room_state(db, room_id="room-1")
    assert service._publish_terminal_tasks(room)

    events = service._events("room-1")
    deferred = next(event for event in events if event["kind"] == "turn.deferred")
    assert deferred["payload"]["task_id"] == first["identity"].task_id
    assert deferred["payload"]["execution_generation"] == 1
    assert service.policy_checkpoint.events_for_task(
        room_id="room-1",
        source_event_seq=int(first["payload"]["source_event_seq"]),
    )

    requeued = service.retry_room_task(
        "room-1",
        task_id=first["identity"].task_id,
    )
    assert requeued["status"] == "queued"
    retried = driver.start_task(
        db,
        first["identity"],
        lease,
        expected_cancel_generation=0,
        clock=clock,
    )
    assert retried.execution_generation == old_attempt.execution_generation + 1


def test_service_refuses_deferred_retry_when_projection_compacts_after_precheck(
    tmp_path: Path,
    monkeypatch,
):
    now = [100.0]

    def clock():
        return now[0]

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.runtime.clock = clock
    service.runtime.lease_ttl_seconds = 30
    service.runtime.indeterminate_defer_seconds = 5
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Compacted room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "default"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-compacted",
        payload={"text": "Check this", "thread_id": "thread-1"},
    )
    first = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    old_lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=service.bindings()[0].gateway_id,
        authority_epoch=1,
        process_generation="offline-member",
        ttl_seconds=1,
        clock=clock,
    )
    driver.start_task(
        db,
        first["identity"],
        old_lease,
        expected_cancel_generation=0,
        clock=clock,
    )

    now[0] = 102.0
    binding = service.bindings()[0]
    service.runtime._process_room(binding)
    now[0] = 108.0
    service.runtime._process_room(binding)
    deferred = driver.list_tasks(db, room_id="room-1", status="deferred")[0]

    source_event_seq = int(deferred["payload"]["source_event_seq"])
    assert service.policy_checkpoint.events_for_task(
        room_id="room-1",
        source_event_seq=source_event_seq,
    )
    original_requeue = driver.requeue_deferred_task

    def compact_then_requeue(*args, **kwargs):
        hosted_rooms.append_event(
            db,
            room_id="room-1",
            event_id="activity-compacted",
            kind="room.activity",
            actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
            payload={
                "status": "settled",
                "reason_code": "silent_round",
                "thread_id": "thread-1",
                "discussion_event_id": "user-compacted",
            },
            authority_gateway_id=str(room["authority_gateway_id"]),
            authority_epoch=int(room["authority_epoch"]),
        )
        latest = hosted_rooms.room_state(db, room_id="room-1")
        service.policy_checkpoint.sync(
            room_id="room-1",
            latest_seq=int(latest["latest_seq"]),
        )
        assert service.policy_checkpoint.events_for_task(
            room_id="room-1",
            source_event_seq=source_event_seq,
        ) == []
        return original_requeue(*args, **kwargs)

    monkeypatch.setattr(driver, "requeue_deferred_task", compact_then_requeue)
    with pytest.raises(
        driver.InvalidTaskTransitionError,
        match="source discussion is no longer active",
    ):
        service.retry_room_task("room-1", task_id=first["identity"].task_id)

    unchanged = driver.list_tasks(db, room_id="room-1", status="deferred")[0]
    assert unchanged["execution_generation"] == deferred["execution_generation"]


def test_stop_fence_prevents_the_next_room_member_from_starting(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    monkeypatch.setattr(service, "local_profiles", lambda: ("default", "ops"))
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "Inspect the release", "thread_id": "thread-1"},
    )
    assert len(driver.list_tasks(db, room_id="room-1")) == 1

    assert service.stop_room("room-1", cancel_id="stop-1") == 1
    service.prepare_room(service.bindings()[0])

    tasks = driver.list_tasks(db, room_id="room-1")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "cancelled"
    assert any(
        event["kind"] == "room.stop_requested" for event in service._events("room-1")
    )


def test_retrying_old_stop_id_does_not_cancel_newer_room_work(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    monkeypatch.setattr(service, "local_profiles", lambda: ("default", "ops"))
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops first", "thread_id": "thread-1"},
    )
    assert service.stop_room("room-1", cancel_id="stop-1") == 1
    service.send(
        room_id="room-1",
        event_id="user-2",
        payload={"text": "@ops newer", "thread_id": "thread-1"},
    )
    newer = driver.list_tasks(db, room_id="room-1", status="queued")[0]

    assert service.stop_room("room-1", cancel_id="stop-1") == 0
    assert driver.get_task(db, newer["identity"])["status"] == "queued"


def test_acknowledged_stop_refuses_to_disband_while_exact_turn_is_still_running(
    tmp_path: Path,
):
    class PendingStopRPC(_FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.active_task_id = None

        def info(self, *, profile, session_id, source):
            return {"active": True, "task_id": self.active_task_id}

        def interrupt_admitted(self, *, task, execution_generation, source):
            return None

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = PendingStopRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    binding = service.bindings()[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="worker",
        ttl_seconds=30,
        clock=time.time,
    )
    driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    rpc.sessions[("ops", "Group: room-1")] = {"session_id": "ops-session"}
    rpc.active_task_id = task["identity"].task_id

    with pytest.raises(RuntimeError, match="still stopping"):
        service.stop_room(
            "room-1",
            cancel_id="stop-1",
            require_acknowledged=True,
        )

    stopping = driver.get_task(db, task["identity"])
    assert stopping["status"] == "stopping"
    assert stopping["cancel_id"] == "stop-1"


def test_demote_retry_stops_newer_turn_before_authority_transfer(
    tmp_path: Path,
    monkeypatch,
):
    from gateway import hosted_room_replicas as replicas

    class ControlledStopRPC(_FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.active_task_id = None
            self.acknowledge = False
            self.expected_task_ids: list[str] = []

        def info(self, *, profile, session_id, source):
            return {
                "active": self.active_task_id is not None,
                "task_id": self.active_task_id,
            }

        def interrupt_admitted(self, *, task, execution_generation, source):
            self.expected_task_ids.append(task.task_id)
            if self.active_task_id != task.task_id:
                return {"found": False, "active": False, "interrupted": False}
            if not self.acknowledge:
                return {"found": True, "active": True, "interrupted": False}
            return {"found": True, "active": True, "interrupted": True}

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = ControlledStopRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    binding = service.bindings()[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation=service.runtime.process_generation,
        process_pid=service.runtime.process_pid,
        process_start_time=service.runtime.process_start_time,
        ttl_seconds=30,
        clock=time.time,
    )
    driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    rpc.sessions[("ops", "Group: room-1")] = {"session_id": "ops-session"}
    rpc.active_task_id = task["identity"].task_id
    observed_gateway = "install:" + "b" * 32
    remote_db = tmp_path / "remote-state.db"
    replicas.ingest_page(
        remote_db,
        room_id="room-1",
        room_name=room["name"],
        members=room["members"],
        page=hosted_rooms.read_events(
            db, room_id="room-1", since_seq=0, limit=100
        ),
    )
    with monkeypatch.context() as remote_gateway:
        remote_gateway.setattr(
            replicas,
            "local_authority_gateway_id",
            lambda: observed_gateway,
        )
        observation = replicas.promote_replica(
            remote_db,
            room_id="room-1",
            reason="old authority unreachable",
        )
    assert observation["authority_epoch"] == 2

    with pytest.raises(RuntimeError, match="still stopping"):
        service.demote_room(
            "room-1",
            observed_gateway_id=observation["authority_gateway_id"],
            observed_epoch=observation["authority_epoch"],
        )

    fenced = hosted_rooms.room_state(db, room_id="room-1")
    assert fenced["authority_gateway_id"] == room["authority_gateway_id"]
    assert fenced["authority_epoch"] == room["authority_epoch"]
    assert not any(
        event["kind"] == "authority.lost" for event in service._events("room-1")
    )

    first_stopping = driver.get_task(db, task["identity"])
    assert first_stopping["status"] == "stopping"
    rpc.active_task_id = None
    first_cancelled = service.runtime.cancel(
        task["identity"],
        cancel_id=first_stopping["cancel_id"],
    )
    assert first_cancelled["status"] == "cancelled"

    service.send(
        room_id="room-1",
        event_id="user-2",
        payload={"text": "@ops inspect again", "thread_id": "thread-2"},
    )
    newer_task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    driver.start_task(
        db,
        newer_task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    rpc.active_task_id = newer_task["identity"].task_id

    with pytest.raises(RuntimeError, match="still stopping"):
        service.demote_room(
            "room-1",
            observed_gateway_id=observation["authority_gateway_id"],
            observed_epoch=observation["authority_epoch"],
        )

    still_local = hosted_rooms.room_state(db, room_id="room-1")
    assert still_local["authority_gateway_id"] == room["authority_gateway_id"]
    assert still_local["authority_epoch"] == room["authority_epoch"]
    assert driver.get_task(db, newer_task["identity"])["status"] == "stopping"

    rpc.acknowledge = True
    result = service.demote_room(
        "room-1",
        observed_gateway_id=observation["authority_gateway_id"],
        observed_epoch=observation["authority_epoch"],
    )

    assert result["authority_gateway_id"] == observed_gateway
    assert result["authority_epoch"] == 2
    assert rpc.expected_task_ids == [
        task["identity"].task_id,
        task["identity"].task_id,
        newer_task["identity"].task_id,
        newer_task["identity"].task_id,
    ]
    stop_ids = [
        event["payload"]["cancel_id"]
        for event in service._events("room-1")
        if event["kind"] == "room.stop_requested"
    ]
    assert len(stop_ids) == 3
    assert len(set(stop_ids)) == 3
    assert any(
        event["kind"] == "authority.lost" for event in service._events("room-1")
    )


def test_cross_process_pending_approval_requires_exact_generation_and_owner_consumes(
    tmp_path: Path,
):
    class ApprovalRPC(_FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.approvals = []
            self.resolved = 0

        def approve(self, *, session_id, request_id, choice):
            self.approvals.append((session_id, request_id, choice))
            return {"resolved": self.resolved}

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = ApprovalRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    binding = service.bindings()[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="worker",
        ttl_seconds=30,
        clock=time.time,
    )
    driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    task = driver.get_task(db, task["identity"])
    service.runtime._report_pending_action(
        task,
        session_id="ops-session",
        info={
            "pending_approval": {
                "request_id": "approval-1",
                "choices": ["once", "always", "deny"],
            }
        },
    )

    action = service.status("room-1")["pending_actions"][0]
    assert action["member_id"] == "ops"
    assert action["approval"]["choices"] == ["once", "deny"]
    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1",
            member_id="ops",
            task_id=task["identity"].task_id,
            execution_generation=1,
            choice="once",
            request_id="wrong-request",
        )

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    dashboard = context.Process(
        target=_approve_room_task_process,
        args=(
            str(db),
            "room-1",
            "ops",
            task["identity"].task_id,
            1,
            "approval-1",
            "once",
            results,
        ),
    )
    dashboard.start()
    dashboard.join(timeout=60)
    try:
        assert not dashboard.is_alive()
        assert dashboard.exitcode == 0
        assert results.get(timeout=5) == {
            "result": {"choice": "once", "idempotent": False}
        }
    finally:
        if dashboard.is_alive():
            dashboard.terminate()
            dashboard.join(timeout=5)
        results.close()
        results.join_thread()
    assert rpc.approvals == []

    # Only the process that owns the live session can wake its local approval
    # queue. Its next observation consumes the durable dashboard decision.
    service.runtime._report_pending_action(
        task,
        session_id="ops-session",
        info={
            "pending_approval": {
                "request_id": "approval-1",
                "choices": ["once", "deny"],
            }
        },
    )
    assert rpc.approvals == [("ops-session", "approval-1", "once")]
    assert service.status("room-1")["pending_actions"]

    rpc.resolved = 1
    service.runtime._report_pending_action(
        task,
        session_id="ops-session",
        info={
            "pending_approval": {
                "request_id": "approval-1",
                "choices": ["once", "deny"],
            }
        },
    )
    assert rpc.approvals == [
        ("ops-session", "approval-1", "once"),
        ("ops-session", "approval-1", "once"),
    ]
    assert service.status("room-1")["pending_actions"] == []
