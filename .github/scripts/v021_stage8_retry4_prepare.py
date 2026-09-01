from __future__ import annotations

import argparse
from pathlib import Path


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolver", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()

    resolver_path = Path(args.resolver)
    candidate = Path(args.candidate)
    prompt_test = candidate / "tests/tui_gateway/test_hosted_room_prompt_fence.py"
    helper_count = sum(
        1
        for line in prompt_test.read_text(encoding="utf-8").splitlines()
        if line.startswith("def _stub_session")
    )
    if helper_count != 1:
        raise RuntimeError(f"expected one _stub_session helper, found {helper_count}")

    text = resolver_path.read_text(encoding="utf-8")
    uniqueness_guard = (
        '    if text.find(start_marker, start + 1) >= 0:\n'
        '        raise RuntimeError(f"{label}: start marker is not unique")\n'
    )
    text = replace_once(
        text,
        uniqueness_guard,
        "",
        label="resolver uniqueness guard",
    )

    helper_anchor = "def require_auto_applied_markers(repo: Path) -> None:\n"
    helpers = r'''def _extract_source_test(
    repo: Path,
    relative: str,
    marker: str,
    next_prefix: str,
) -> str:
    incoming = output(repo, "show", f"{FIRST}:{relative}")
    start = incoming.find(marker)
    if start < 0:
        raise RuntimeError(f"source commit lost regression: {marker}")
    end = incoming.find(next_prefix, start + len(marker))
    if end < 0:
        end = len(incoming)
    return incoming[start:end].rstrip() + "\n\n"


def ensure_profile_deleted_test(repo: Path) -> None:
    relative = "tests/tui_gateway/test_hosted_room_service.py"
    path = repo / relative
    current = path.read_text(encoding="utf-8")
    marker = "def test_profile_deleted_after_planning_is_deferred_before_admission"
    if marker in current:
        return
    regression = _extract_source_test(repo, relative, marker, "\ndef ")
    anchor = "def test_demotion_interrupts_inflight_turn_before_authority_changes"
    if current.count(anchor) != 1:
        raise RuntimeError("expected one demotion-test insertion anchor")
    current = current.replace(anchor, regression + anchor, 1)
    ast.parse(current, filename=str(path))
    path.write_text(current, encoding="utf-8")
    git(repo, "add", "--", relative)


def ensure_profile_availability_deferral(repo: Path) -> None:
    relative = "tui_gateway/hosted_room_service.py"
    path = repo / relative
    current = path.read_text(encoding="utf-8")
    marker = "current_profiles = self.local_profiles()"
    if marker in current:
        return

    import_anchor = "import time\nfrom collections"
    if current.count(import_anchor) != 1:
        raise RuntimeError("expected one service import anchor")
    current = current.replace(
        import_anchor,
        "import time\nimport uuid\nfrom collections",
        1,
    )

    plan_anchor = """            if plan is None:
                return

            identity = driver.TaskIdentity(
"""
    deferral = """            if plan is None:
                return

            if plan.target.kind == "local":
                current_profiles = self.local_profiles()
                if plan.target.profile not in current_profiles:
                    missing_profile = plan.target.profile
                    result = self.hosted_store.append_event(
                        self.db_path,
                        room_id=room_id,
                        event_id=(
                            f"member-unavailable:{plan.user_event_id}:"
                            f"{plan.member_id}:{uuid.uuid4()}"
                        ),
                        event_type="turn.deferred",
                        actor_id=plan.member_id,
                        payload={
                            "thread_id": plan.thread_id,
                            "source_user_event_id": plan.user_event_id,
                            "member_id": plan.member_id,
                            "target_profile": missing_profile,
                            "reason": "member_unavailable",
                            "retryable": True,
                        },
                        expected_epoch=state.authority_epoch,
                        expected_authority_gateway_id=(
                            state.authority_gateway_id
                        ),
                        expected_next_seq=state.next_seq,
                    )
                    if not result.applied:
                        continue
                    self._raise_if_stop_fenced(state)
                    continue

            identity = driver.TaskIdentity(
"""
    if current.count(plan_anchor) != 1:
        raise RuntimeError("expected one service admission anchor")
    current = current.replace(plan_anchor, deferral, 1)
    ast.parse(current, filename=str(path))
    path.write_text(current, encoding="utf-8")
    git(repo, "add", "--", relative)


def ensure_hosted_room_identity_persistence(repo: Path) -> None:
    relative = "tui_gateway/server.py"
    path = repo / relative
    current = path.read_text(encoding="utf-8")
    marker = 'model_config["hosted_room_id"]'
    if marker in current:
        return
    anchor = (
        '    if session.get("room_plumbing"):\n'
        '        model_config["room_plumbing"] = True\n'
    )
    addition = (
        anchor
        + '    if hosted_room_id := str(session.get("hosted_room_id") or "").strip():\n'
        + '        model_config["hosted_room_id"] = hosted_room_id\n'
    )
    if current.count(anchor) != 1:
        raise RuntimeError("expected one room-plumbing persistence anchor")
    current = current.replace(anchor, addition, 1)
    ast.parse(current, filename=str(path))
    path.write_text(current, encoding="utf-8")
    git(repo, "add", "--", relative)


def ensure_hosted_room_identity_persistence_test(repo: Path) -> None:
    relative = "tests/tui_gateway/test_custom_provider_session_persistence.py"
    path = repo / relative
    current = path.read_text(encoding="utf-8")
    marker = "    def test_ensure_db_row_persists_hosted_room_identity"
    if marker in current:
        return
    regression = _extract_source_test(repo, relative, marker, "\n    def ")
    anchor = "    def test_ensure_db_row_omits_marker_without_contract"
    if current.count(anchor) != 1:
        raise RuntimeError("expected one persistence-test insertion anchor")
    current = current.replace(anchor, regression + anchor, 1)
    ast.parse(current, filename=str(path))
    path.write_text(current, encoding="utf-8")
    git(repo, "add", "--", relative)


'''
    text = replace_once(
        text,
        helper_anchor,
        helpers + helper_anchor,
        label="auto-applied marker helper",
    )

    call_anchor = "    require_auto_applied_markers(repo)\n\n    remaining ="
    call_replacement = (
        "    ensure_profile_deleted_test(repo)\n"
        "    ensure_profile_availability_deferral(repo)\n"
        "    ensure_hosted_room_identity_persistence(repo)\n"
        "    ensure_hosted_room_identity_persistence_test(repo)\n"
        "    require_auto_applied_markers(repo)\n\n"
        "    remaining ="
    )
    text = replace_once(
        text,
        call_anchor,
        call_replacement,
        label="auto-applied marker call",
    )

    resolver_path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
