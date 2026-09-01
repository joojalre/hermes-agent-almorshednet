from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

START = "da58db5b2630a4338280601837ed16c5a4b40d62"
FIRST = "b306ba41e62057d4f5d51c70493fdad0c8ae75df"
REST = """60767cb87f3c532f003adc6448ec7aa6e1e925d4
28118f12865779ba02eb70d108bb49580322544a
87dec320debe42f6288d89327ee5d84669d8a2cf
36ac9d4be0a9222b20eba30e4f973d0d21eab4d3
b1ea144797a10c440b166d7b3cb79e24817b8173
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


def require_conflict(repo: Path, source: str) -> None:
    result = git(repo, "cherry-pick", "--no-commit", source, check=False)
    if result.returncode == 0:
        raise RuntimeError(f"{source}: expected a conflict")


def resolve_diff3(path: Path, resolver) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    rendered: list[str] = []
    index = 0
    block = 0
    while index < len(lines):
        if not lines[index].startswith("<<<<<<< "):
            rendered.append(lines[index])
            index += 1
            continue

        block += 1
        index += 1
        ours: list[str] = []
        while index < len(lines) and not lines[index].startswith("||||||| "):
            ours.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RuntimeError(f"{path}: missing diff3 base marker")

        index += 1
        base: list[str] = []
        while index < len(lines) and not lines[index].startswith("======="):
            base.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RuntimeError(f"{path}: missing separator")

        index += 1
        incoming: list[str] = []
        while index < len(lines) and not lines[index].startswith(">>>>>>> "):
            incoming.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RuntimeError(f"{path}: missing end marker")
        index += 1

        replacement = resolver(
            block,
            "".join(ours),
            "".join(base),
            "".join(incoming),
        )
        if replacement and not replacement.endswith("\n"):
            replacement += "\n"
        rendered.append(replacement)

    text = "".join(rendered)
    if any(marker in text for marker in ("<<<<<<<", "|||||||", ">>>>>>>")):
        raise RuntimeError(f"{path}: unresolved conflict marker")
    path.write_text(text, encoding="utf-8")


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
        raise RuntimeError("candidate branch moved before stage 4")
    if output(repo, "status", "--porcelain"):
        raise RuntimeError("candidate branch is dirty before stage 4")

    git(repo, "config", "user.name", "Hermes v0.21.0 Reconciler")
    git(repo, "config", "user.email", "v021-reconciler@invalid.local")
    git(repo, "config", "merge.conflictStyle", "diff3")
    git(
        repo,
        "fetch",
        "--no-tags",
        "origin",
        "refs/pull/19/head:refs/remotes/origin/pr19-head",
    )

    applied: list[tuple[str, str]] = []
    empty: list[str] = []

    require_conflict(repo, FIRST)
    driver_path = repo / "tui_gateway/hosted_room_driver.py"

    def combine_queue_lock(
        block: int,
        ours: str,
        _base: str,
        _incoming: str,
    ) -> str:
        if block != 1:
            raise RuntimeError(f"unexpected stage-4 conflict block {block}")
        marker = "\n    @staticmethod\n"
        if marker not in ours:
            raise RuntimeError("route retry helpers moved")
        _old_loop, helpers = ours.split(marker, 1)
        combined_loop = """            if self._route_retry_is_deferred(task):
                return
            profile = task["payload"]["target_profile"]
            # Keep the durable task queued until both its remote route and
            # local profile lock are available. Do not mark it running before
            # the model's serialization lock is owned.
            with self.turn_lock(profile):
                lease = self._renew_lease_if_needed(binding, lease)
                attempt = state.start_task(
                    self.db_path,
                    task["identity"],
                    lease,
                    expected_cancel_generation=task["cancel_generation"],
                    clock=self.clock,
                )
                self._execute_attempt(
                    binding,
                    task,
                    attempt,
                    turn_lock_held=True,
                )
            current = state.get_task(self.db_path, task["identity"])
            if current["status"] not in state.TERMINAL_STATUSES:
                return
"""
        return combined_loop + marker + helpers

    resolve_diff3(driver_path, combine_queue_lock)
    git(repo, "add", "tui_gateway/hosted_room_driver.py")
    if output(repo, "diff", "--name-only", "--diff-filter=U"):
        raise RuntimeError("stage-4 conflict remains unresolved")
    ast.parse(driver_path.read_text(encoding="utf-8"), filename=str(driver_path))
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
        raise RuntimeError("candidate branch is dirty after stage 4")

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
