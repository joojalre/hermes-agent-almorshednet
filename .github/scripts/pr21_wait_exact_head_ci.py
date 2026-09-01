from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request(
    *,
    method: str,
    path: str,
    token: str,
    body: bytes | None = None,
) -> tuple[int, dict[str, object] | None]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hermes-pr21-exact-head-ci",
    }
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read()
            decoded = json.loads(payload.decode("utf-8")) if payload else None
            return response.status, decoded
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API {method} {path} failed: {exc.code} {payload}"
        ) from exc


def main() -> None:
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    head_sha = os.environ["EXACT_HEAD_SHA"]
    report_dir = Path(os.environ["REPORT_DIR"])
    report_dir.mkdir(parents=True, exist_ok=True)
    required = {
        ".github/workflows/ci.yaml": "CI",
        ".github/workflows/nix.yml": "Nix flake check",
    }
    evidence: dict[str, object] = {
        "repository": repository,
        "head_sha": head_sha,
        "required": required,
        "approvals": [],
        "polls": [],
    }

    def list_runs() -> list[dict[str, object]]:
        query = urllib.parse.urlencode(
            {
                "head_sha": head_sha,
                "event": "pull_request",
                "per_page": 100,
            }
        )
        _, payload = request(
            method="GET",
            path=f"/repos/{repository}/actions/runs?{query}",
            token=token,
        )
        assert payload is not None
        return list(payload.get("workflow_runs", []))

    selected: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        current = list_runs()
        for workflow_path in required:
            matches = [
                run for run in current if run.get("path") == workflow_path
            ]
            if matches:
                matches.sort(
                    key=lambda run: str(run.get("created_at") or ""),
                    reverse=True,
                )
                selected[workflow_path] = matches[0]
        if set(selected) == set(required):
            break
        time.sleep(10)
    if set(selected) != set(required):
        missing = sorted(set(required) - set(selected))
        raise RuntimeError(f"exact-head workflow runs did not appear: {missing}")

    approvals = evidence["approvals"]
    assert isinstance(approvals, list)
    for workflow_path, run in selected.items():
        run_id = int(run["id"])
        item: dict[str, object] = {
            "path": workflow_path,
            "name": required[workflow_path],
            "run_id": run_id,
            "initial_status": run.get("status"),
            "initial_conclusion": run.get("conclusion"),
        }
        if run.get("conclusion") == "action_required":
            status, _ = request(
                method="POST",
                path=f"/repos/{repository}/actions/runs/{run_id}/approve",
                token=token,
                body=b"{}",
            )
            if status not in {201, 204}:
                raise RuntimeError(
                    f"unexpected approval response for run {run_id}: {status}"
                )
            item["approval_http_status"] = status
        approvals.append(item)

    terminal: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + 6600
    while time.monotonic() < deadline:
        current = list_runs()
        snapshot: list[dict[str, object]] = []
        for workflow_path, name in required.items():
            matches = [
                run for run in current if run.get("path") == workflow_path
            ]
            if not matches:
                continue
            matches.sort(
                key=lambda run: str(run.get("created_at") or ""),
                reverse=True,
            )
            run = matches[0]
            state = {
                "path": workflow_path,
                "name": name,
                "run_id": int(run["id"]),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "run_attempt": run.get("run_attempt"),
            }
            snapshot.append(state)
            if (
                run.get("status") == "completed"
                and run.get("conclusion") != "action_required"
            ):
                terminal[workflow_path] = state
        polls = evidence["polls"]
        assert isinstance(polls, list)
        polls.append(snapshot)
        (report_dir / "progress.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if set(terminal) == set(required):
            break
        time.sleep(15)

    if set(terminal) != set(required):
        missing = sorted(set(required) - set(terminal))
        raise RuntimeError(
            f"exact-head workflows did not reach terminal state: {missing}"
        )
    failures = {
        workflow_path: state
        for workflow_path, state in terminal.items()
        if state.get("conclusion") != "success"
    }
    evidence["terminal"] = terminal
    evidence["failures"] = failures
    (report_dir / "final.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(terminal, indent=2, sort_keys=True))
    if failures:
        raise RuntimeError(f"exact-head workflow failures: {failures}")


if __name__ == "__main__":
    main()
