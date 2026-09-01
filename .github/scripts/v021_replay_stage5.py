from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

START = "9e8c3b0af258d313eb740a54f5f80f7acbb6c51e"
FIRST = "36ac9d4be0a9222b20eba30e4f973d0d21eab4d3"
REST = """b1ea144797a10c440b166d7b3cb79e24817b8173
327696f2770fcbcf711c954ec97533525bf98c76
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


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and process.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} ({process.returncode})\n"
            f"{process.stdout}\n{process.stderr}"
        )
    return process


def output(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def commit(repo: Path, source: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "--no-gpg-sign", "--no-verify", "-C", source)
    return output(repo, "rev-parse", "HEAD")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def require_conflict(repo: Path, source: str, expected: set[str]) -> None:
    result = git(repo, "cherry-pick", "--no-commit", source, check=False)
    if result.returncode == 0:
        raise RuntimeError(f"{source}: expected a conflict")
    actual = {
        line
        for line in output(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
        if line
    }
    if actual != expected:
        raise RuntimeError(
            f"{source}: unexpected conflict set: expected={sorted(expected)} actual={sorted(actual)}"
        )


def resolve_discussion(repo: Path) -> None:
    relative = "gateway/hosted_room_discussion.py"
    git(repo, "checkout", "--ours", "--", relative)
    path = repo / relative
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        """def _validate_member_target(
    value: Any,
    *,
    profile: str,
    known_profiles: set[str],
    index: int,
) -> dict[str, Any]:
""",
        """def _validate_member_target(
    value: Any,
    *,
    profile: str,
    known_profiles: set[str],
    require_current_profiles: bool,
    index: int,
) -> dict[str, Any]:
""",
        label="member target signature",
    )
    text = replace_once(
        text,
        """        if profile not in known_profiles:
""",
        """        if require_current_profiles and profile not in known_profiles:
""",
        label="implicit local membership",
    )
    text = replace_once(
        text,
        """        if target_profile != profile or profile not in known_profiles:
""",
        """        if target_profile != profile or (
            require_current_profiles and profile not in known_profiles
        ):
""",
        label="explicit local membership",
    )
    text = replace_once(
        text,
        """def validate_roster(
    value: Any,
    *,
    local_profiles: Iterable[str],
) -> tuple[DiscussionMember, ...]:
    \"\"\"Validate a frozen 2-6 member roster of profiles on this gateway.\"\"\"
""",
        """def validate_roster(
    value: Any,
    *,
    local_profiles: Iterable[str],
    require_current_profiles: bool = True,
) -> tuple[DiscussionMember, ...]:
    \"\"\"Validate a frozen 2-6 member roster of local or peer targets.

    Room creation requires each local target to exist on this gateway. Replay
    preserves the stored roster without reapplying that time-varying check, so
    an unavailable local member can be deferred while healthy local and peer
    members continue.
    \"\"\"
""",
        label="roster signature",
    )
    text = replace_once(
        text,
        """            known_profiles=known_profiles,
            index=index,
""",
        """            known_profiles=known_profiles,
            require_current_profiles=require_current_profiles,
            index=index,
""",
        label="target validation call",
    )
    text = replace_once(
        text,
        """    members = validate_roster(value.get(\"members\"), local_profiles=local_profiles)
""",
        """    members = validate_roster(
        value.get(\"members\"),
        local_profiles=local_profiles,
        require_current_profiles=False,
    )
""",
        label="room replay roster",
    )

    ast.parse(text, filename=str(path))
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "--", relative)


def resolve_driver(repo: Path) -> None:
    relative = "tui_gateway/hosted_room_driver.py"
    git(repo, "checkout", "--ours", "--", relative)
    path = repo / relative
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        """import contextlib
import threading
""",
        """import contextlib
import hashlib
import threading
""",
        label="hashlib import",
    )
    text = replace_once(
        text,
        """ROOM_SESSION_SOURCE = \"bot_room\"
MAX_TERMINAL_TEXT_BYTES = 64 * 1024
""",
        """ROOM_SESSION_SOURCE = \"bot_room\"
MAX_TERMINAL_TEXT_BYTES = 64 * 1024
_ROOM_SESSION_TITLE_PREFIX = \"Group: \"
_ROOM_SESSION_TITLE_MAX_CHARS = 100
""",
        label="room title constants",
    )
    text = replace_once(
        text,
        """def room_session_title(room_id: str) -> str:
    \"\"\"Return the canonical hidden session title for one hosted room.\"\"\"
    return f\"Group: {room_id}\"
""",
        """def room_session_title(room_id: str) -> str:
    \"\"\"Return the bounded canonical hidden-session title for one room.\"\"\"
    title = f\"{_ROOM_SESSION_TITLE_PREFIX}{room_id}\"
    if len(title) <= _ROOM_SESSION_TITLE_MAX_CHARS:
        return title

    digest = hashlib.sha256(room_id.encode(\"utf-8\")).hexdigest()
    room_prefix_chars = (
        _ROOM_SESSION_TITLE_MAX_CHARS
        - len(_ROOM_SESSION_TITLE_PREFIX)
        - 1
        - len(digest)
    )
    return f\"{_ROOM_SESSION_TITLE_PREFIX}{room_id[:room_prefix_chars]}~{digest}\"
""",
        label="bounded room title",
    )

    ast.parse(text, filename=str(path))
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "--", relative)


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
            result = subprocess.run(
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
            (conflict_dir / name).write_text(result.stdout or "", encoding="utf-8")


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
    arguments = parser.parse_args()

    repo = Path(arguments.repo).resolve()
    report = Path(arguments.report).resolve()
    report.mkdir(parents=True, exist_ok=True)

    if output(repo, "rev-parse", "HEAD") != START:
        raise RuntimeError("candidate branch moved before stage 5")
    if output(repo, "status", "--porcelain"):
        raise RuntimeError("candidate branch is dirty before stage 5")

    git(repo, "config", "user.name", "Hermes v0.21.0 Reconciler")
    git(repo, "config", "user.email", "v021-reconciler@invalid.local")
    git(
        repo,
        "fetch",
        "--no-tags",
        "origin",
        "refs/pull/19/head:refs/remotes/origin/pr19-head",
    )

    applied: list[tuple[str, str]] = []
    empty: list[str] = []

    require_conflict(
        repo,
        FIRST,
        {
            "gateway/hosted_room_discussion.py",
            "tui_gateway/hosted_room_driver.py",
        },
    )
    resolve_discussion(repo)
    resolve_driver(repo)
    remaining = output(repo, "diff", "--name-only", "--diff-filter=U")
    if remaining:
        raise RuntimeError(f"stage-5 conflicts remain unresolved: {remaining}")
    applied.append((FIRST, commit(repo, FIRST)))

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
        raise RuntimeError("candidate branch is dirty after stage 5")

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
