"""Restore literal session routes without waiting for profile secret-source I/O."""


def resume_runtime_overrides(row: dict | None, profile_home) -> dict:
    from tui_gateway import server

    overrides = literal_session_runtime_overrides(row)
    if overrides is not None:
        return overrides
    with server._profile_build_scope(profile_home):
        return server._stored_session_runtime_overrides(row)


def assemble_runtime_overrides(model, provider, base_url, api_mode, reasoning_config, service_tier) -> dict:
    """Shared representation for stored routes and the config-dependent recovery path."""
    overrides = {}
    if model:
        overrides["model_override"] = {
            "model": model, "provider": provider or None, "base_url": base_url or None, "api_mode": api_mode or None}
    if provider:
        overrides["provider_override"] = provider
    if isinstance(reasoning_config, dict):
        overrides["reasoning_config_override"] = reasoning_config
    if service_tier:  # None inherits the profile; empty string explicitly disables priority.
        overrides["service_tier_override"] = "" if service_tier.lower() == "normal" else service_tier
    return overrides


def _has_env_reference(value) -> bool:
    if isinstance(value, str):
        return "${" in value
    if isinstance(value, dict):
        return any(_has_env_reference(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_env_reference(v) for v in value)
    return False


def literal_session_runtime_overrides(row: dict | None) -> dict | None:
    """Return persisted overrides only when profile config cannot change their interpretation.

    None delegates to the hydrated path: composer provenance and stale custom-provider healing
    must see resolved profile config. Agent construction still binds the full owning profile scope.
    """
    from tui_gateway import server

    if not row or server._row_follows_profile(row):
        return None
    config = server._parse_model_config(row.get("model_config"), quiet=True)
    if (config.get("room_plumbing") or "composer_override_profile" in config
            or (row.get("hidden") and str(row.get("title") or "").strip().startswith("Group:"))):
        return None
    model = str(row.get("model") or config.get("model") or "").strip()
    if not model:
        return None
    from hermes_state import SessionDB
    from hermes_cli.auth import BUILTIN_PROVIDER_IDS
    from hermes_cli.providers import HERMES_OVERLAYS, normalize_provider
    from hermes_cli.runtime_provider import is_foreign_provider_endpoint

    route = SessionDB.session_gateway_runtime(row)
    provider, base_url, api_mode = (str(route.get(k) or "").strip() for k in ("provider", "base_url", "api_mode"))
    # These built-ins resolve even without a catalog. Plugin/custom names can require profile config.
    if provider and (provider not in BUILTIN_PROVIDER_IDS or normalize_provider(provider) not in HERMES_OVERLAYS):
        return None
    reasoning_config = config.get("reasoning_config")
    service_tier = str(config.get("service_tier") or "").strip()
    if _has_env_reference((model, provider, base_url, api_mode, reasoning_config, service_tier)):
        return None
    if is_foreign_provider_endpoint(provider, base_url):
        base_url = api_mode = ""
    return assemble_runtime_overrides(model, provider, base_url, api_mode, reasoning_config, service_tier)
