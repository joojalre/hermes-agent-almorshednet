from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


def top_level_nodes(text: str) -> dict[str, str]:
    tree = ast.parse(text)
    result: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result[node.name] = type(node).__name__
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = type(node).__name__
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[node.target.id] = type(node).__name__
    return result


def signatures(text: str) -> dict[str, str]:
    tree = ast.parse(text)
    result: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = [arg.arg for arg in node.args.args]
        kwonly = [arg.arg for arg in node.args.kwonlyargs]
        result[node.name] = json.dumps(
            {
                "args": arguments,
                "kwonly": kwonly,
                "vararg": node.args.vararg.arg if node.args.vararg else None,
                "kwarg": node.args.kwarg.arg if node.args.kwarg else None,
            },
            sort_keys=True,
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    current_path = Path(args.current)
    final_path = Path(args.final)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    current = current_path.read_text(encoding="utf-8")
    final = final_path.read_text(encoding="utf-8")
    current_nodes = top_level_nodes(current)
    final_nodes = top_level_nodes(final)
    current_signatures = signatures(current)
    final_signatures = signatures(final)

    shared_functions = sorted(set(current_signatures) & set(final_signatures))
    changed_signatures = {
        name: {
            "current": current_signatures[name],
            "final": final_signatures[name],
        }
        for name in shared_functions
        if current_signatures[name] != final_signatures[name]
    }
    summary = {
        "current_only_nodes": sorted(set(current_nodes) - set(final_nodes)),
        "final_only_nodes": sorted(set(final_nodes) - set(current_nodes)),
        "changed_function_signatures": changed_signatures,
        "current_line_count": len(current.splitlines()),
        "final_line_count": len(final.splitlines()),
        "required_final_markers": {
            marker: marker in final
            for marker in (
                "process_pid",
                "process_start_time",
                "hosted_room_driver_admission_barriers",
                "hosted_room_driver_demotion_intents",
                "TaskAdmissionBlockedError",
                "def block_room_admissions(",
                "def begin_room_demotion(",
                "def admit_task(",
            )
        },
        "required_current_release_markers": {
            marker: marker in current
            for marker in (
                "target_member_id",
                "def publish_approval_request(",
                "def list_pending_approval_requests(",
                "def record_terminal_receipt(",
            )
        },
    }
    (output / "ast-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
