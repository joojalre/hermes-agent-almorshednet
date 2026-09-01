from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

START = "c19e98fbf5e0ceb243d066e3e46a4f304fb8d606"
SUPERSEDED = "327696f2770fcbcf711c954ec97533525bf98c76"
SUPERSEDED_PARENTS = {
    "36ac9d4be0a9222b20eba30e4f973d0d21eab4d3",
    "b1ea144797a10c440b166d7b3cb79e24817b8173",
}
REST = """e3fd97d9781aec7efc6ce6cd5d291dc81b024bd1
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


def require_markers(repo: Path, relative: str, markers: tuple[str, ...]) -> None:
    text = (repo / relative).read_text(encoding="utf-8")
    for marker in markers:
        if marker not in text:
            raise RuntimeError(f"{relative}: missing supersession marker: {marker}")


def verify_superseded_merge(repo: Path, report: Path) -> None:
    parent_line = output(repo, "rev-list", "--parents", "-n1", SUPERSEDED).split()
    parents = set(parent_line[1:])
    if parents != SUPERSEDED_PARENTS:
        raise RuntimeError(
            f"unexpected parents for {SUPERSEDED}: {sorted(parents)}"
        )

    require_markers(
        repo,
        "gateway/hosted_room_discussion.py",
        (
            "require_current_profiles: bool = True",
            "require_current_profiles=False",
            "target_type = \"local\"",
            "target_type = \"peer\"",
        ),
    )
    discussion = (repo / "gateway/hosted_room_discussion.py").read_text(
        encoding="utf-8"
    )
    if discussion.count("require_current_profiles=False") < 4:
        raise RuntimeError("not all persisted-room replay paths bypass current-profile checks")

    require_markers(
        repo,
        "gateway/hosted_room_driver.py",
        (
            "approval request requires a running task generation",
            "approval request task is no longer running",
            "AND tasks.status='running'",
        ),
    )
    require_markers(
        repo,
        "gateway/hosted_room_replicas.py",
        ("def validate_demotion_observation(",),
    )
    require_markers(
        repo,
        "hermes_cli/web_server.py",
        (
            "hosted_room_start_allowed = threading.Event()",
            "await asyncio.to_thread(hosted_room_start_thread.join, timeout=1.0)",
            "_hosted_groups.stop_hosted_room_service,",
        ),
    )
    require_markers(
        repo,
        "tui_gateway/hosted_room_driver.py",
        (
            "digest = hashlib.sha256(room_id.encode(\"utf-8\")).hexdigest()",
            "state.requeue_not_admitted_task(",
            "if submit_attempted and bool(getattr(exc, \"not_admitted\", False)):",
        ),
    )
    require_markers(
        repo,
        "tui_gateway/hosted_room_service.py",
        (
            "def demote_room(",
            "with self._policy_lock:",
            "require_acknowledged=True",
            "commit_demotion",
        ),
    )
    require_markers(
        repo,
        "tui_gateway/methods_groups.py",
        (
            '@method("groups.demote")',
            "service = get_hosted_room_service()",
            "result = service.demote_room(",
        ),
    )
    require_markers(
        repo,
        "tests/gateway/test_hosted_room_driver.py",
        ("def test_approval_requests_are_stale_once_task_is_stopping",),
    )
    require_markers(
        repo,
        "tests/hermes_cli/test_web_server_boot_handshake.py",
        ("def test_hosted_room_shutdown_does_not_block_event_loop",),
    )
    require_markers(
        repo,
        "tests/tui_gateway/test_hosted_room_driver_runtime.py",
        (
            "def test_room_session_title_bounds_max_room_id_and_preserves_uniqueness",
            "def test_proven_not_admitted_submission_settles_without_ambiguity",
        ),
    )

    evidence = {
        "commit": SUPERSEDED,
        "parents": sorted(parents),
        "decision": "superseded_by_separately_reconciled_parents",
        "candidate_head": output(repo, "rev-parse", "HEAD"),
    }
    (report / "superseded-327696f.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
    tracked = output(repo, "ls-tree", "-r", "--name-only", "HEAD")
    if "hermes-update-mutex" in tracked or "/var/folders/" in tracked:
        raise RuntimeError("temporary macOS updater paths leaked into candidate")

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
        raise RuntimeError("candidate branch moved before stage 7")
    if output(repo, "status", "--porcelain"):
        raise RuntimeError("candidate branch is dirty before stage 7")

    git(repo, "config", "user.name", "Hermes v0.21.0 Reconciler")
    git(repo, "config", "user.email", "v021-reconciler@invalid.local")
    git(
        repo,
        "fetch",
        "--no-tags",
        "origin",
        "refs/pull/19/head:refs/remotes/origin/pr19-head",
    )

    verify_superseded_merge(repo, report)

    applied: list[tuple[str, str]] = []
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
        raise RuntimeError("candidate branch is dirty after stage 7")

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
        "superseded_commit": SUPERSEDED,
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
