from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

START = "c19e98fbf5e0ceb243d066e3e46a4f304fb8d606"
FIRST = "e3fd97d9781aec7efc6ce6cd5d291dc81b024bd1"
REST = """acf3708e2475ca5f868bfc7326c5f76289177bb0
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
    "tests/tui_gateway/test_hosted_room_prompt_fence.py",
    "tui_gateway/methods_prompt.py",
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


def replace_section(
    text: str,
    start_marker: str,
    end_marker: str,
    replacement: str,
    *,
    label: str,
) -> str:
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError(f"{label}: start marker missing")
    end = text.find(end_marker, start)
    if end < 0:
        raise RuntimeError(f"{label}: end marker missing")
    if text.find(start_marker, start + 1) >= 0:
        raise RuntimeError(f"{label}: start marker is not unique")
    return text[:start] + replacement + text[end:]


def checkout_ours(repo: Path, relative: str) -> tuple[Path, str]:
    git(repo, "checkout", "--ours", "--", relative)
    path = repo / relative
    return path, path.read_text(encoding="utf-8")


def write_python(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    ast.parse(text, filename=str(path))
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "--", relative)


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


def resolve_methods_prompt(repo: Path) -> None:
    relative = "tui_gateway/methods_prompt.py"
    _path, text = checkout_ours(repo, relative)

    proof_anchor = '''        ) or not isinstance(hosted_task.get("execution_generation"), int):
            return _err(rid, 4120, "invalid hosted room turn proof")
    else:
'''
    proof_replacement = '''        ) or not isinstance(hosted_task.get("execution_generation"), int):
            return _err(rid, 4120, "invalid hosted room turn proof")
        session["hosted_room_id"] = hosted_task["room_id"]
    else:
'''
    text = replace_once(
        text,
        proof_anchor,
        proof_replacement,
        label="bind live hosted room identity",
    )

    start_marker = '''        # Older Desktop builds know the `Group: <room-id>` session title but
'''
    end_marker = '''    if (limit_message := _ensure_active_session_slot(sid, session)) is not None:
'''
    replacement = '''        # Older Desktop builds know the `Group: <room-id>` session title but
        # not the hosted authority marker. Prefer the canonical identity bound
        # by the hosted runtime or persisted in the session row; a bounded
        # presentation title is not a reversible room identifier.
        room_id = str(session.get("hosted_room_id") or "").strip()
        session_key = str(session.get("session_key") or "").strip()
        if not room_id and session_key:
            try:
                with _session_db(session) as session_db:
                    if session_db is not None:
                        room_id = str(
                            session_db.get_session_model_config_value(
                                session_key,
                                "hosted_room_id",
                                "",
                            )
                            or ""
                        ).strip()
            except Exception:
                room_id = ""

        title = str(session.get("title") or "")
        if not room_id and title.startswith("Group: "):
            room_id = title.removeprefix("Group: ").strip()

        if room_id:
            try:
                from gateway.hosted_rooms import (
                    HostedRoomError,
                    RoomProbeUnavailableError,
                    default_db_path,
                    probe_hosted_room,
                    probe_peer_room_reservation,
                )

                hosted = probe_hosted_room(default_db_path(), room_id=room_id)
                peer = False
                if not hosted:
                    from hermes_constants import named_profile_home

                    session_profile_home = named_profile_home(
                        str(session.get("profile_home") or "")
                    )
                    requested_profile = (
                        (
                            session_profile_home.name
                            if session_profile_home is not None
                            else ""
                        )
                        or str(params.get("profile") or "").strip()
                        or str(_current_profile_name() or "default").strip()
                    )
                    peer = probe_peer_room_reservation(
                        default_db_path(),
                        room_id=room_id,
                        target_profile=requested_profile,
                    )
            except RoomProbeUnavailableError:
                return _err(
                    rid,
                    5122,
                    "Could not verify this group. Try again after the gateway recovers.",
                )
            except HostedRoomError:
                # Legacy Desktop sessions used the display name after
                # "Group: "; those names are not hosted room ids.
                pass
            except Exception:
                return _err(
                    rid,
                    5122,
                    "Could not verify this group. Try again after the gateway recovers.",
                )
            else:
                if hosted or peer:
                    return _err(
                        rid,
                        4122,
                        (
                            "This room is managed by its gateway. "
                            if hosted
                            else "This room is managed by its home host. "
                        )
                        + "Update Hermes Desktop to continue it.",
                    )
'''
    text = replace_section(
        text,
        start_marker,
        end_marker,
        replacement,
        label="hosted room prompt fence",
    )

    required = (
        "probe_peer_room_reservation",
        "_respond_compute_host_clarify",
        "Failed to persist the session. Storage is unavailable.",
        'session["hosted_room_id"] = hosted_task["room_id"]',
        "get_session_model_config_value",
    )
    for marker in required:
        if marker not in text:
            raise RuntimeError(f"methods_prompt stronger marker missing: {marker}")
    write_python(repo, relative, text)


def resolve_prompt_fence_tests(repo: Path) -> None:
    relative = "tests/tui_gateway/test_hosted_room_prompt_fence.py"
    _path, text = checkout_ours(repo, relative)

    text = replace_once(
        text,
        "import time\n\nimport pytest\n",
        "import time\nfrom contextlib import contextmanager\n\nimport pytest\n",
        label="contextmanager import",
    )
    text = replace_once(
        text,
        "from gateway import hosted_rooms\nimport tui_gateway.server as server\n",
        "from gateway import hosted_rooms\nfrom tui_gateway.hosted_room_driver import room_session_title\nimport tui_gateway.server as server\n",
        label="room title import",
    )

    helper_start = "def _stub_session(monkeypatch, *, title, profile_home=None):\n"
    helper_end = "\n\ndef test_direct_prompt_to_hosted_group_session_is_rejected"
    helper = '''def _stub_session(
    monkeypatch,
    *,
    title,
    profile_home=None,
    hosted_room_id=None,
    session_key=None,
):
    session = {
        "id": "session-1",
        "title": title,
        "source": "bot_room",
        "profile_home": str(profile_home) if profile_home else None,
    }
    if hosted_room_id is not None:
        session["hosted_room_id"] = hosted_room_id
    if session_key is not None:
        session["session_key"] = session_key
    monkeypatch.setattr(
        server,
        "_sess_nowait",
        lambda _params, _rid: (session, None),
    )
'''
    text = replace_section(
        text,
        helper_start,
        helper_end,
        helper,
        label="session stub helper",
    )

    insertion_anchor = '''def test_direct_prompt_to_non_hosted_group_reaches_normal_admission(
'''
    long_identity_tests = '''def test_direct_prompt_uses_bound_room_id_for_bounded_session_title(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    room_id = "room-" + "x" * 120
    hosted_rooms.create_room(
        hosted_rooms.default_db_path(),
        room_id=room_id,
        name="Long hosted room",
        members=[],
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
    )
    _stub_session(
        monkeypatch,
        title=room_session_title(room_id),
        hosted_room_id=room_id,
    )

    result = server._methods["prompt.submit"](
        "request-long", {"session_id": "session-1", "text": "continue"}
    )

    assert result["error"]["code"] == 4122


def test_direct_prompt_recovers_persisted_room_id_for_bounded_title(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    room_id = "room-" + "y" * 120
    hosted_rooms.create_room(
        hosted_rooms.default_db_path(),
        room_id=room_id,
        name="Persisted long room",
        members=[],
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
    )

    class FakeSessionDB:
        def get_session_model_config_value(self, session_id, key, default=None):
            assert (session_id, key) == (
                "stored-room-session",
                "hosted_room_id",
            )
            return room_id

    @contextmanager
    def session_db(_session):
        yield FakeSessionDB()

    monkeypatch.setattr(server, "_session_db", session_db)
    _stub_session(
        monkeypatch,
        title=room_session_title(room_id),
        session_key="stored-room-session",
    )

    result = server._methods["prompt.submit"](
        "request-persisted-long",
        {"session_id": "session-1", "text": "continue"},
    )

    assert result["error"]["code"] == 4122


'''
    text = replace_once(
        text,
        insertion_anchor,
        long_identity_tests + insertion_anchor,
        label="long room identity tests",
    )

    required = (
        "def test_direct_prompt_to_peer_reserved_group_is_rejected_until_revoke",
        "def test_direct_prompt_uses_bound_room_id_for_bounded_session_title",
        "def test_direct_prompt_recovers_persisted_room_id_for_bounded_title",
        "def test_direct_prompt_is_refused_when_room_authority_cannot_be_verified",
    )
    for marker in required:
        if marker not in text:
            raise RuntimeError(f"prompt fence test marker missing: {marker}")
    write_python(repo, relative, text)


def require_auto_applied_markers(repo: Path) -> None:
    checks = {
        "tests/tui_gateway/test_custom_provider_session_persistence.py": (
            "def test_ensure_db_row_persists_hosted_room_identity",
        ),
        "tests/tui_gateway/test_hosted_room_service.py": (
            "def test_profile_deleted_after_planning_is_deferred_before_admission",
        ),
        "tui_gateway/hosted_room_service.py": (
            "current_profiles = self.local_profiles()",
            '"reason": "member_unavailable"',
            "uuid.uuid4()",
        ),
        "tui_gateway/server.py": (
            "hosted_room_id",
            "model_config",
        ),
    }
    for relative, markers in checks.items():
        text = (repo / relative).read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                raise RuntimeError(f"{relative}: missing applied marker: {marker}")
        ast.parse(text, filename=str(repo / relative))


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
    tracked = set(output(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    release_tracked = set(
        output(repo, "ls-tree", "-r", "--name-only", RELEASE).splitlines()
    )
    introduced_temporary_paths = sorted(
        candidate_path
        for candidate_path in tracked - release_tracked
        if "hermes-update-mutex" in candidate_path or "/var/folders/" in candidate_path
    )
    if introduced_temporary_paths:
        raise RuntimeError(
            "candidate introduced temporary macOS updater paths: "
            + ", ".join(introduced_temporary_paths)
        )

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
        raise RuntimeError("candidate branch moved before stage 8")
    if output(repo, "status", "--porcelain"):
        raise RuntimeError("candidate branch is dirty before stage 8")

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
    resolve_methods_prompt(repo)
    resolve_prompt_fence_tests(repo)
    require_auto_applied_markers(repo)

    remaining = output(repo, "diff", "--name-only", "--diff-filter=U")
    if remaining:
        raise RuntimeError(f"stage-8 conflicts remain unresolved: {remaining}")

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
        raise RuntimeError("candidate branch is dirty after stage 8")

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
