"""A selectorless Windows update restores the root multiplexer, not a sticky profile."""

import pytest

import hermes_constants
from hermes_cli import gateway, gateway_multiplex_mode, gateway_windows, main, profiles, update_cmd, update_cmd_windows
from hermes_cli import update_pause_record


pytestmark = pytest.mark.platforms("windows")


@pytest.fixture
def cold_start_scope(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    alpha = root / "profiles" / "alpha"
    alpha.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(alpha))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    monkeypatch.setattr(main, "explicit_cli_profile", lambda: None)
    monkeypatch.setattr(gateway_multiplex_mode, "default_gateway_multiplexes", lambda *_a: True)
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_kw: [])
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: True)
    calls = []

    def record(operation, result):
        def call(*_a, **_kw):
            calls.append((operation, gateway_windows._hermes_home()))
            return result
        return call

    monkeypatch.setattr(gateway_windows, "is_installed", record("installed", True))
    monkeypatch.setattr(gateway_windows, "attested_death_generation", record("generation", "dead-root"))
    monkeypatch.setattr(gateway_windows, "_spawn_detached", record("spawn", 4242))
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", record("ready", [4242]))
    monkeypatch.setattr(gateway_windows, "_consume_start_attestation", record("consume", None))
    monkeypatch.setattr(gateway_windows, "_write_start_attestation", record("attest", None))
    return root, alpha, calls, record


def test_sticky_profile_cold_start_binds_plan_and_all_recovery_operations_to_root(cold_start_scope):
    root, alpha, calls, _record = cold_start_scope
    token = update_cmd_windows._windows_cold_start_plan()
    assert token is not None
    assert update_cmd._cold_start_windows_gateway_after_update(token) is True
    assert calls == [(name, root) for name in ("installed", "generation", "spawn", "ready", "consume", "attest")]
    assert hermes_constants.get_hermes_home() == alpha


@pytest.mark.parametrize("profile,multiplex,custom", [("alpha", True, False), (None, False, False), (None, True, True)])
def test_explicit_profile_nonmultiplexer_and_custom_home_keep_their_own_scope(
    cold_start_scope, monkeypatch, tmp_path, profile, multiplex, custom
):
    _root, alpha, calls, _record = cold_start_scope
    target = tmp_path / "custom-home" if custom else alpha
    monkeypatch.setenv("HERMES_HOME", str(target))
    monkeypatch.setattr(main, "explicit_cli_profile", lambda: profile)
    monkeypatch.setattr(gateway_multiplex_mode, "default_gateway_multiplexes", lambda *_a: multiplex)
    token = update_cmd_windows._windows_cold_start_plan()
    assert update_cmd._cold_start_windows_gateway_after_update(token) is True
    assert calls == [(name, target) for name in ("installed", "generation", "spawn", "ready", "consume", "attest")]
    assert hermes_constants.get_hermes_home() == target


def test_cold_start_failure_restores_caller_scope_and_keeps_recovery_authority(cold_start_scope, monkeypatch):
    root, alpha, calls, record = cold_start_scope
    token = update_cmd_windows._windows_cold_start_plan()
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", record("ready", []))
    with pytest.raises(RuntimeError, match="did not become ready"):
        update_cmd._cold_start_windows_gateway_after_update(token)
    assert calls == [(name, root) for name in ("installed", "generation", "spawn", "ready")]
    assert token["attested_generation"] == "dead-root"
    assert hermes_constants.get_hermes_home() == alpha


def test_resumed_plan_keeps_its_recorded_home_after_profile_selection_changes(cold_start_scope, monkeypatch):
    root, _alpha, calls, _record = cold_start_scope
    token = update_cmd_windows._windows_cold_start_plan()
    beta = root / "profiles" / "beta"
    monkeypatch.setenv("HERMES_HOME", str(beta))
    monkeypatch.setattr(main, "explicit_cli_profile", lambda: "beta")
    assert update_cmd._cold_start_windows_gateway_after_update(token) is True
    assert calls == [(name, root) for name in ("installed", "generation", "spawn", "ready", "consume", "attest")]
    assert hermes_constants.get_hermes_home() == beta


def test_sibling_attestations_skip_the_planned_home_instead_of_the_sticky_profile(cold_start_scope, monkeypatch):
    root, alpha, _calls, _record = cold_start_scope
    token = update_cmd_windows._windows_cold_start_plan()
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "profiles_to_serve", lambda **_kw: [("default", root), ("alpha", alpha)])
    probes = []

    def generation(_pids, *, home):
        probes.append(home)
        return "dead-alpha"

    monkeypatch.setattr(gateway_windows, "attested_death_generation", generation)
    update_cmd_windows._record_attested_cold_start_profiles(token, set())
    assert probes == [alpha]
    assert token["cold_start_profiles"] == {"alpha": "dead-alpha"}


@pytest.mark.parametrize("current,adopted,expected_home", [
    ({}, {"attested_generation": "old", "cold_start_home": "root"}, "root"),
    ({"attested_generation": "current", "cold_start_home": "root"},
     {"attested_generation": "old", "cold_start_home": "root"}, "root"),
    ({"attested_generation": "current"}, {"attested_generation": "old", "cold_start_home": "root"}, None),
    ({}, {"attested_generation": "legacy"}, None),
])
def test_adoption_keeps_home_and_generation_paired_with_legacy_precedence(current, adopted, expected_home):
    merged = update_pause_record.merge_into(
        {"cold_start_if_installed": True, **current}, {"cold_start_if_installed": True, **adopted}
    )
    assert merged.get("cold_start_home") == expected_home
    assert merged["attested_generation"] == current.get("attested_generation", adopted["attested_generation"])


def test_adoption_does_not_graft_another_homes_generation_onto_current_plan():
    current = {"cold_start_if_installed": True, "cold_start_home": "alpha"}
    adopted = {"cold_start_if_installed": True, "cold_start_home": "root", "attested_generation": "dead-root"}
    with pytest.raises(update_pause_record.RecordConflict, match="cold-start homes"):
        update_pause_record.merge_into(current, adopted)
    assert current == {"cold_start_if_installed": True, "cold_start_home": "alpha"}
    assert adopted == {"cold_start_if_installed": True, "cold_start_home": "root", "attested_generation": "dead-root"}


def test_discharged_plan_fields_do_not_override_the_adopted_home_and_generation():
    merged = update_pause_record.merge_into(
        {"cold_start_if_installed": False, "cold_start_home": "alpha", "attested_generation": "done-alpha"},
        {"cold_start_if_installed": True, "cold_start_home": "root", "attested_generation": "dead-root"},
    )
    assert merged["cold_start_if_installed"] is True
    assert (merged["cold_start_home"], merged["attested_generation"]) == ("root", "dead-root")
