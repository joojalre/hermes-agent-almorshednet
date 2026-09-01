from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

START = "f159e7855ed45c5a4efb8809407c310f7910618c"
FIRST = "b1ea144797a10c440b166d7b3cb79e24817b8173"
REST = """327696f2770fcbcf711c954ec97533525bf98c76
e3fd97d9781aec7efc6ce6cd5d291dc81b024bd1
acf3708e2475ca5f868bfc7326c5f76289177bb0
5af994bf71b710955ea4710a2198067b253ab3b6
e7d3e23562ee996a25c5e6ff025ef0fde90fe6a5
d3b9806383d10ee063333f39f1b8c4023ad9a685
e5780d2e9c9e40a14214fc2712e750aec6989c9e
37be945a99611cf3b5589746726e376b8164bcb6
d1ef0ea7d033e915ed1089c4612edbfdc4f445e1
c1875cad92ff6b005cb2523ee1bc89b116b5c2cd
4431c43453e8586ca1a8a42fae5b339d661fa126
27cbc9bbbb9ade9c9fe13d6549a2acf6b8292993
10618eb0979f4c3af980f8ce380d9c0fb06fc5ca
7c6a0c8e648b441bf89ecdef5e53c8410cd0d174
6db80964f7c25c99e577f6d550ad0568f833a5dd
b7f1acdef0308ac75f09c3aa41702686e420b599
20d0a6a42365b2b2351e1dca819022b6ec477b39""".split()
RELEASE = "29112bef099274229cadff79cdff7bf7b99c4b77"
CONFLICTS = {
    "gateway/hosted_room_discussion.py",
    "gateway/hosted_room_driver.py",
    "hermes_cli/web_server.py",
    "tests/gateway/test_hosted_room_driver.py",
    "tests/hermes_cli/test_web_server_boot_handshake.py",
    "tests/tui_gateway/test_hosted_room_driver_runtime.py",
    "tui_gateway/hosted_room_driver.py",
    "tui_gateway/hosted_room_service.py",
    "tui_gateway/methods_groups.py",
}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} ({proc.returncode})\n{proc.stdout}\n{proc.stderr}"
        )
    return proc


def output(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def replace_count(text: str, old: str, new: str, *, count: int, label: str) -> str:
    actual = text.count(old)
    if actual != count:
        raise RuntimeError(f"{label}: expected {count} matches, found {actual}")
    return text.replace(old, new)


def checkout_ours(repo: Path, relative: str) -> tuple[Path, str]:
    git(repo, "checkout", "--ours", "--", relative)
    path = repo / relative
    return path, path.read_text(encoding="utf-8")


def write_python(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    ast.parse(text, filename=str(path))
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "--", relative)


def resolve_discussion(repo: Path) -> None:
    relative = "gateway/hosted_room_discussion.py"
    _path, text = checkout_ours(repo, relative)
    text = replace_once(
        text,
        """def validate_room(
    value: Any,
    *,
    local_profiles: Iterable[str],
) -> DiscussionRoom:
""",
        """def validate_room(
    value: Any,
    *,
    local_profiles: Iterable[str],
    require_current_profiles: bool = True,
) -> DiscussionRoom:
""",
        label="validate_room signature",
    )
    text = replace_once(
        text,
        """    members = validate_roster(
        value.get("members"),
        local_profiles=local_profiles,
        require_current_profiles=False,
    )
""",
        """    members = validate_roster(
        value.get("members"),
        local_profiles=local_profiles,
        require_current_profiles=require_current_profiles,
    )
""",
        label="validate_room roster mode",
    )
    text = replace_count(
        text,
        """    room = validate_room(room_value, local_profiles=local_profiles)
""",
        """    room = validate_room(
        room_value,
        local_profiles=local_profiles,
        require_current_profiles=False,
    )
""",
        count=4,
        label="replay validate_room calls",
    )
    write_python(repo, relative, text)


def resolve_gateway_driver(repo: Path) -> None:
    relative = "gateway/hosted_room_driver.py"
    _path, text = checkout_ours(repo, relative)
    text = replace_once(
        text,
        """        if task["status"] not in {"running", "stopping"}:
            raise InvalidTaskTransitionError(
                "approval request requires an active task generation"
            )
""",
        """        if task["status"] != "running":
            raise InvalidTaskTransitionError(
                "approval request requires a running task generation"
            )
""",
        label="publish approval running fence",
    )
    text = replace_once(
        text,
        """        if task["status"] != "running":
            raise StaleTaskError("approval decision requires a running task")
""",
        """        if task["status"] != "running":
            raise StaleTaskError("approval request task is no longer running")
""",
        label="approval decision message",
    )
    text = replace_once(
        text,
        """                 AND tasks.status IN ('running', 'stopping')
""",
        """                 AND tasks.status='running'
""",
        label="pending approval query fence",
    )
    write_python(repo, relative, text)


def keep_stronger_web_server(repo: Path) -> None:
    relative = "hermes_cli/web_server.py"
    _path, text = checkout_ours(repo, relative)
    required = (
        "hosted_room_start_allowed = threading.Event()",
        "await asyncio.to_thread(hosted_room_start_thread.join, timeout=1.0)",
        "_hosted_groups.stop_hosted_room_service,",
    )
    for marker in required:
        if marker not in text:
            raise RuntimeError(f"web_server stronger lifecycle marker missing: {marker}")
    write_python(repo, relative, text)


def add_approval_test(repo: Path) -> None:
    relative = "tests/gateway/test_hosted_room_driver.py"
    _path, text = checkout_ours(repo, relative)
    if "def test_approval_requests_are_stale_once_task_is_stopping" not in text:
        anchor = "def test_release_fails_closed_while_its_task_is_running(db):\n"
        test = '''def test_approval_requests_are_stale_once_task_is_stopping(db):
    clock = FakeClock()
    identity = _identity()
    lease = _lease(db, clock)
    _admit(db, identity, clock)
    attempt = driver.start_task(
        db,
        identity,
        lease,
        expected_cancel_generation=0,
        clock=clock,
    )
    driver.publish_approval_request(
        db,
        identity,
        execution_generation=attempt.execution_generation,
        member_id="member-ops",
        request_id="approval-1",
        session_id="session-1",
        action={"tool": "shell", "command": "inspect"},
        clock=clock,
    )

    driver.begin_task_cancel(
        db,
        identity,
        cancel_id="cancel-before-approval",
        expected_cancel_generation=0,
        clock=clock,
    )

    assert driver.list_pending_approval_requests(db, room_id="room-1") == []
    with pytest.raises(driver.StaleTaskError, match="no longer running"):
        driver.decide_approval_request(
            db,
            identity,
            execution_generation=attempt.execution_generation,
            member_id="member-ops",
            request_id="approval-1",
            choice="once",
            clock=clock,
        )
    with pytest.raises(driver.InvalidTaskTransitionError, match="running task"):
        driver.publish_approval_request(
            db,
            identity,
            execution_generation=attempt.execution_generation,
            member_id="member-ops",
            request_id="approval-2",
            session_id="session-1",
            action={"tool": "shell", "command": "inspect again"},
            clock=clock,
        )


'''
        text = replace_once(text, anchor, test + anchor, label="approval test anchor")
    write_python(repo, relative, text)


def add_shutdown_test(repo: Path) -> None:
    relative = "tests/hermes_cli/test_web_server_boot_handshake.py"
    _path, text = checkout_ours(repo, relative)
    if "def test_hosted_room_shutdown_does_not_block_event_loop" not in text:
        anchor = "# ---------------------------------------------------------------------------\n# Test 2 — get_status run_in_executor keeps event loop free for other requests\n"
        test = '''def test_hosted_room_shutdown_does_not_block_event_loop(monkeypatch):
    from tui_gateway import methods_groups

    stop_started = threading.Event()
    loop_progressed = threading.Event()
    progress_observed_while_stopping = threading.Event()

    def blocked_stop(*, timeout):
        assert timeout == 5.0
        stop_started.set()
        if loop_progressed.wait(timeout=2.0):
            progress_observed_while_stopping.set()
        return True

    monkeypatch.setattr(web_server_mod, "_warm_gateway_module", lambda: None)
    monkeypatch.setattr(
        methods_groups,
        "start_hosted_room_service",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(methods_groups, "stop_hosted_room_service", blocked_stop)

    async def exercise_shutdown():
        lifespan = web_server_mod._lifespan(web_server_mod.app)
        await lifespan.__aenter__()

        async def observe_worker_start():
            while not stop_started.is_set():
                await asyncio.sleep(0)
            loop_progressed.set()

        observer = asyncio.create_task(observe_worker_start())
        await lifespan.__aexit__(None, None, None)
        await observer

        assert stop_started.is_set()
        assert progress_observed_while_stopping.is_set()

    asyncio.run(exercise_shutdown())


'''
        text = replace_once(text, anchor, test + anchor, label="shutdown test anchor")
    write_python(repo, relative, text)


def add_title_test(repo: Path) -> None:
    relative = "tests/tui_gateway/test_hosted_room_driver_runtime.py"
    _path, text = checkout_ours(repo, relative)
    if "def test_room_session_title_bounds_max_room_id_and_preserves_uniqueness" not in text:
        anchor = "def test_local_crash_recovery_keeps_ambiguous_history_explicit_without_resume(\n"
        test = '''def test_room_session_title_bounds_max_room_id_and_preserves_uniqueness():
    assert room_session_title("room-1") == "Group: room-1"

    shared_prefix = "r" * 127
    first = room_session_title(f"{shared_prefix}a")
    second = room_session_title(f"{shared_prefix}b")

    assert len(first) <= 100
    assert len(second) <= 100
    assert first.startswith("Group: ")
    assert second.startswith("Group: ")
    assert first != second
    assert len(first.rsplit("~", 1)[1]) == 64
    assert len(second.rsplit("~", 1)[1]) == 64


'''
        text = replace_once(text, anchor, test + anchor, label="title test anchor")
    if "def test_proven_not_admitted_submission_settles_without_ambiguity" not in text:
        raise RuntimeError("current not-admitted regression disappeared")
    write_python(repo, relative, text)


def keep_stronger_runtime(repo: Path) -> None:
    relative = "tui_gateway/hosted_room_driver.py"
    _path, text = checkout_ours(repo, relative)
    required = (
        "digest = hashlib.sha256(room_id.encode(\"utf-8\")).hexdigest()",
        "self._requeue_not_admitted_task(attempt)",
        "if submit_attempted and getattr(exc, \"not_admitted\", False):",
    )
    for marker in required:
        if marker not in text:
            raise RuntimeError(f"runtime stronger marker missing: {marker}")
    write_python(repo, relative, text)


def keep_stronger_service(repo: Path) -> None:
    relative = "tui_gateway/hosted_room_service.py"
    _path, text = checkout_ours(repo, relative)
    required = (
        "def demote_room(",
        "with self._policy_lock:",
        "require_acknowledged=True",
        "commit_demotion",
    )
    for marker in required:
        if marker not in text:
            raise RuntimeError(f"service stronger marker missing: {marker}")
    write_python(repo, relative, text)


def keep_stronger_groups(repo: Path) -> None:
    relative = "tui_gateway/methods_groups.py"
    _path, text = checkout_ours(repo, relative)
    required = (
        '@method("groups.demote")',
        "service = get_hosted_room_service()",
        "return _err(rid, 4123, _WORKER_UNAVAILABLE)",
        "result = service.demote_room(",
    )
    for marker in required:
        if marker not in text:
            raise RuntimeError(f"groups stronger marker missing: {marker}")
    write_python(repo, relative, text)


def require_conflict(repo: Path) -> None:
    result = git(repo, "cherry-pick", "--no-commit", FIRST, check=False)
    if result.returncode == 0:
        raise RuntimeError(f"{FIRST}: expected a conflict")
    actual = {
        line
        for line in output(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
        if line
    }
    if actual != CONFLICTS:
        raise RuntimeError(
            f"{FIRST}: unexpected conflict set: expected={sorted(CONFLICTS)} actual={sorted(actual)}"
        )


def commit(repo: Path, source: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "--no-gpg-sign", "--no-verify", "-C", source)
    return output(repo, "rev-parse", "HEAD")


def capture_conflict(repo: Path, report: Path, source: str) -> None:
    destination = report / "next-conflict"
    (destination / "files").mkdir(parents=True, exist_ok=True)
    paths = [
        line
        for line in output(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
        if line
    ]
    (destination / "commit.txt").write_text(source + "\n", encoding="utf-8")
    (destination / "files.txt").write_text(
        "".join(path + "\n" for path in paths), encoding="utf-8"
    )
    (destination / "status.txt").write_text(
        output(repo, "status", "--short") + "\n", encoding="utf-8"
    )
    for relative in paths:
        conflict_dir = destination / "files" / relative
        conflict_dir.mkdir(parents=True, exist_ok=True)
        for stage, name in (("1", "base"), ("2", "current"), ("3", "incoming")):
            result = git(repo, "show", f":{stage}:{relative}", check=False)
            (conflict_dir / name).write_text(result.stdout or "", encoding="utf-8")
        for left, right, name in (
            ("base", "current", "base-to-current.diff"),
            ("base", "incoming", "base-to-incoming.diff"),
            ("current", "incoming", "current-to-incoming.diff"),
        ):
            proc = subprocess.run(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--histogram",
                    "--",
                    str(conflict_dir / left),
                    str(conflict_dir / right),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (conflict_dir / name).write_text(proc.stdout or "", encoding="utf-8")


def verify_candidate(repo: Path) -> int:
    git(repo, "diff", f"{RELEASE}..HEAD", "--check")
    paths = [
        repo / line
        for line in output(
            repo, "diff", "--name-only", f"{RELEASE}..HEAD", "--", "*.py"
        ).splitlines()
        if line and (repo / line).is_file()
    ]
    for path in paths:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return len(paths)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    report = Path(args.report).resolve()
    report.mkdir(parents=True, exist_ok=True)

    if output(repo, "rev-parse", "HEAD") != START:
        raise RuntimeError("candidate branch moved before stage 6")
    if output(repo, "status", "--porcelain"):
        raise RuntimeError("candidate branch is dirty before stage 6")

    git(repo, "config", "user.name", "Hermes v0.21.0 Reconciler")
    git(repo, "config", "user.email", "v021-reconciler@invalid.local")
    git(
        repo,
        "fetch",
        "--no-tags",
        "origin",
        "refs/pull/19/head:refs/remotes/origin/pr19-head",
    )

    require_conflict(repo)
    resolve_discussion(repo)
    resolve_gateway_driver(repo)
    keep_stronger_web_server(repo)
    add_approval_test(repo)
    add_shutdown_test(repo)
    add_title_test(repo)
    keep_stronger_runtime(repo)
    keep_stronger_service(repo)
    keep_stronger_groups(repo)

    remaining = output(repo, "diff", "--name-only", "--diff-filter=U")
    if remaining:
        raise RuntimeError(f"stage-6 conflicts remain unresolved: {remaining}")

    applied: list[tuple[str, str]] = [(FIRST, commit(repo, FIRST))]
    empty: list[str] = []
    failed = ""
    for source in REST:
        parent_count = len(output(repo, "rev-list", "--parents", "-n1", source).split()) - 1
        command = ["cherry-pick", "--no-commit"]
        if parent_count > 1:
            command.extend(["-m", "1"])
        command.append(source)
        result = git(repo, *command, check=False)
        if result.returncode:
            failed = source
            capture_conflict(repo, report, source)
            git(repo, "cherry-pick", "--abort", check=False)
            git(repo, "reset", "--hard", "HEAD")
            break
        if not output(repo, "diff", "--cached", "--name-only") and not output(
            repo, "diff", "--name-only"
        ):
            empty.append(source)
            continue
        applied.append((source, commit(repo, source)))

    if output(repo, "status", "--porcelain"):
        raise RuntimeError("candidate branch is dirty after stage 6")

    compiled = verify_candidate(repo)
    project_version = next(
        (
            match.group(1)
            for line in (repo / "pyproject.toml").read_text(encoding="utf-8").splitlines()
            if (match := re.fullmatch(r'version = "([^"]+)"', line))
        ),
        "",
    )
    cli_match = re.search(
        r'^__version__ = "([^"]+)"',
        (repo / "hermes_cli/__init__.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    cli_version = cli_match.group(1) if cli_match else ""
    if (project_version, cli_version) != ("0.21.0", "0.21.0"):
        raise RuntimeError(f"unexpected versions: {project_version}/{cli_version}")

    (report / "applied.tsv").write_text(
        "".join(f"{source}\t{rebased}\n" for source, rebased in applied),
        encoding="utf-8",
    )
    (report / "empty.txt").write_text(
        "".join(source + "\n" for source in empty), encoding="utf-8"
    )
    summary = {
        "start": START,
        "candidate_head": output(repo, "rev-parse", "HEAD"),
        "candidate_tree": output(repo, "rev-parse", "HEAD^{tree}"),
        "applied_count": len(applied),
        "empty_count": len(empty),
        "failed_commit": failed,
        "project_version": project_version,
        "cli_version": cli_version,
        "changed_python_compiled": compiled,
    }
    (report / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
