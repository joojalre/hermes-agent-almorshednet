from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path

PRODUCTION_FILE = Path("gateway/hosted_rooms.py")
SOURCE_COMMITS = (
    "10618eb0979f4c3af980f8ce380d9c0fb06fc5ca",
    "7c6a0c8e648b441bf89ecdef5e53c8410cd0d174",
    "6db80964f7c25c99e577f6d550ad0568f833a5dd",
    "b7f1acdef0308ac75f09c3aa41702686e420b599",
)
RELEASE_MARKERS = (
    "from typing import Any, Iterator, Mapping, NoReturn",
    "def _legacy_members_match(",
    "def reserve_peer_room(",
    "def remote_run_receipt(",
    "def record_remote_run_receipt(",
)
PATCH_MARKERS = (
    "_DISCUSSION_LIABILITY_PREFIX",
    "def _terminal_publication_liabilities(",
    "def _closing_discussion_liability_keys(",
    "def _is_terminal_recovery_plan(",
    "require_open_admissions: bool = False",
    "prospective_liability_keys",
    "released_liability_keys",
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


def verify_python(*, stage: str) -> None:
    text = PRODUCTION_FILE.read_text(encoding="utf-8")
    ast.parse(text, filename=str(PRODUCTION_FILE))
    for marker in RELEASE_MARKERS:
        if marker not in text:
            raise RuntimeError(f"{stage}: release marker disappeared: {marker}")


def apply_commit_delta(commit: str, *, index: int, report_dir: Path) -> dict[str, object]:
    parent = git("rev-parse", f"{commit}^").stdout.strip()
    patch = git(
        "diff",
        "--binary",
        parent,
        commit,
        "--",
        str(PRODUCTION_FILE),
    ).stdout
    if not patch.strip():
        raise RuntimeError(f"{commit}: no storage delta")

    prefix = f"{index:02d}-{commit[:12]}"
    patch_path = report_dir / f"{prefix}.patch"
    log_path = report_dir / f"{prefix}.apply.log"
    patch_path.write_text(patch, encoding="utf-8")

    process = git("apply", "--3way", "--index", str(patch_path), check=False)
    log_path.write_text(
        f"returncode={process.returncode}\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}",
        encoding="utf-8",
    )
    if process.returncode:
        raise RuntimeError(
            f"{commit}: sequential storage patch did not apply\n"
            f"{process.stdout}\n{process.stderr}"
        )

    verify_python(stage=commit)
    changed = [
        line
        for line in git("diff", "--cached", "--name-only").stdout.splitlines()
        if line
    ]
    if changed != [str(PRODUCTION_FILE)]:
        raise RuntimeError(f"{commit}: unexpected staged paths: {changed}")
    return {
        "commit": commit,
        "parent": parent,
        "patch_bytes": len(patch.encode("utf-8")),
        "staged_paths": changed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    if git("status", "--porcelain").stdout.strip() not in {
        "?? tests/gateway/test_hosted_room_storage_reservations.py"
    }:
        raise RuntimeError("unexpected working tree before sequential storage apply")

    verify_python(stage="candidate-before-apply")
    applied = [
        apply_commit_delta(commit, index=index, report_dir=report_dir)
        for index, commit in enumerate(SOURCE_COMMITS, start=1)
    ]

    text = PRODUCTION_FILE.read_text(encoding="utf-8")
    for marker in PATCH_MARKERS:
        if marker not in text:
            raise RuntimeError(f"final storage invariant marker missing: {marker}")
    for marker in RELEASE_MARKERS:
        if marker not in text:
            raise RuntimeError(f"final release marker missing: {marker}")

    git("diff", "--check")
    summary = {
        "source_commits": list(SOURCE_COMMITS),
        "applied": applied,
        "release_markers_preserved": list(RELEASE_MARKERS),
        "patch_markers_present": list(PATCH_MARKERS),
        "staged_diff_stat": git("diff", "--cached", "--stat").stdout.strip(),
    }
    (report_dir / "sequential-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
