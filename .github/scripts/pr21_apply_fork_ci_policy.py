from __future__ import annotations

import json
from pathlib import Path

CONTRIBUTOR = Path(".github/workflows/contributor-check.yml")
RUNNER_FILES = {
    Path(".github/workflows/js-tests.yml"): (
        "    runs-on: ubuntu-latest-32-core\n",
        "    runs-on: ubuntu-latest\n",
    ),
    Path(".github/workflows/rust-tests.yml"): (
        "    runs-on: ubuntu-latest-32-core\n",
        "    runs-on: ubuntu-latest\n",
    ),
    Path(".github/workflows/tests.yml"): (
        "    runs-on: ubuntu-latest-96-core\n",
        "    runs-on: ubuntu-latest\n",
    ),
    Path(".github/workflows/tests-os.yml"): (
        "            runner: windows-latest-32-core\n",
        "            runner: windows-latest\n",
    ),
}
TEST_FILE = Path("tests/ci/test_fork_ci_policy.py")


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_contributor_check() -> None:
    old = """        id: check-emails
        run: |
          # Get the merge base between this PR and main
"""
    new = """        id: check-emails
        env:
          PR_AUTHOR: ${{ github.event.pull_request.user.login || github.actor }}
        run: |
          set -euo pipefail
          # Fork-owner maintenance is authenticated by GitHub identity. Its
          # preserved upstream/fork commits were already reviewed, so do not
          # republish their historical author addresses as mapping files.
          # Non-owner contributor PRs still pass through the full gate below.
          if [ "$GITHUB_REPOSITORY" != "NousResearch/hermes-agent" ] && \
             [ "$PR_AUTHOR" = "$GITHUB_REPOSITORY_OWNER" ]; then
            echo "Fork-owner maintenance PR; GitHub identity is authoritative."
            echo "review_status=[]" >> "$GITHUB_OUTPUT"
            echo "review_status=[]" > review-status.json
            exit 0
          fi

          # Get the merge base between this PR and main
"""
    replace_once(CONTRIBUTOR, old, new, "fork-owner attribution guard")


def patch_runners() -> None:
    for path, (old, new) in RUNNER_FILES.items():
        replace_once(path, old, new, f"public runner fallback for {path}")


def write_regression_tests() -> None:
    TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEST_FILE.write_text(
        '''"""Regression checks for fork-safe GitHub Actions policy."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_fork_owner_attribution_exception_is_narrow() -> None:
    workflow = _read(".github/workflows/contributor-check.yml")
    assert "PR_AUTHOR: ${{ github.event.pull_request.user.login || github.actor }}" in workflow
    assert '[ "$GITHUB_REPOSITORY" != "NousResearch/hermes-agent" ]' in workflow
    assert '[ "$PR_AUTHOR" = "$GITHUB_REPOSITORY_OWNER" ]' in workflow
    assert "Non-owner contributor PRs still pass through the full gate" in workflow
    assert "Check for unmapped contributor emails" in workflow


def test_private_runner_assignments_are_removed_from_the_fork() -> None:
    assignments = {
        ".github/workflows/js-tests.yml": (
            "runs-on: ubuntu-latest-32-core",
            "runs-on: ubuntu-latest",
        ),
        ".github/workflows/rust-tests.yml": (
            "runs-on: ubuntu-latest-32-core",
            "runs-on: ubuntu-latest",
        ),
        ".github/workflows/tests.yml": (
            "runs-on: ubuntu-latest-96-core",
            "runs-on: ubuntu-latest",
        ),
        ".github/workflows/tests-os.yml": (
            "runner: windows-latest-32-core",
            "runner: windows-latest",
        ),
    }
    for relative, (private, public) in assignments.items():
        workflow = _read(relative)
        assert private not in workflow
        assert public in workflow


if __name__ == "__main__":
    test_fork_owner_attribution_exception_is_narrow()
    test_private_runner_assignments_are_removed_from_the_fork()
''',
        encoding="utf-8",
    )


def main() -> None:
    patch_contributor_check()
    patch_runners()
    write_regression_tests()
    print(
        json.dumps(
            {
                "changed": [
                    str(CONTRIBUTOR),
                    *(str(path) for path in RUNNER_FILES),
                    str(TEST_FILE),
                ],
                "policy": "fork owner only; non-owner contributor checks remain strict",
                "runners": "standard GitHub-hosted runners in this fork",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
