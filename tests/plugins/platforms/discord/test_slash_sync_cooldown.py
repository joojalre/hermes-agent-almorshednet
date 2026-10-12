"""Slash-sync 429 cooldown and fingerprint decisions survive adapter recreation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord import adapter as discord_platform


class _DesiredCommand:
    def __init__(self, description):
        self.description = description

    def to_dict(self, tree):
        return {"name": "status", "description": self.description, "type": 1, "options": []}


def _adapter(description):
    adapter = discord_platform.DiscordAdapter(PlatformConfig(enabled=True))
    command = _DesiredCommand(description)
    adapter._client = SimpleNamespace(
        application_id=999,
        tree=SimpleNamespace(
            get_commands=lambda: [command],
            fetch_commands=AsyncMock(return_value=[]),
        ),
        http=SimpleNamespace(
            max_ratelimit_timeout=77,
            upsert_global_command=AsyncMock(),
        ),
    )
    return adapter, command


@pytest.mark.asyncio
async def test_persisted_429_cooldown_precedes_changed_fingerprint_and_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DISCORD_COMMAND_SYNC_POLICY", "safe")
    clock = {"now": 100.0}
    monkeypatch.setattr(discord_platform.time, "time", lambda: clock["now"])

    first, _ = _adapter("Initial description")
    fingerprint = first._desired_command_sync_fingerprint()
    first._client.http.upsert_global_command.side_effect = discord.HTTPException(
        SimpleNamespace(status=429, reason="Too Many Requests", headers={"Retry-After": "20"}),
        {"message": "Synthetic rate limit", "code": 0, "retry_after": 20.0},
    )

    await first._run_post_connect_initialization()

    first._client.tree.fetch_commands.assert_awaited_once()
    first._client.http.upsert_global_command.assert_awaited_once()
    assert first._client.http.max_ratelimit_timeout == 77
    state_path = first._command_sync_state_path()
    persisted = state_path.read_bytes()
    limited = json.loads(persisted)["999"]
    assert limited["fingerprint"] == fingerprint
    assert limited["retry_after"] == 20.0
    assert limited["retry_after_until"] == 120.0
    assert "last_success_at" not in limited

    recreated, _ = _adapter("Changed during cooldown")
    changed_fingerprint = recreated._desired_command_sync_fingerprint()
    assert changed_fingerprint != fingerprint
    clock["now"] = 119.999

    await recreated._run_post_connect_initialization()

    recreated._client.tree.fetch_commands.assert_not_awaited()
    recreated._client.http.upsert_global_command.assert_not_awaited()
    assert state_path.read_bytes() == persisted

    clock["now"] = 120.0
    await recreated._run_post_connect_initialization()

    recreated._client.tree.fetch_commands.assert_awaited_once()
    recreated._client.http.upsert_global_command.assert_awaited_once()
    assert recreated._client.http.max_ratelimit_timeout == 77
    successful = json.loads(state_path.read_bytes())["999"]
    assert successful["fingerprint"] == changed_fingerprint
    assert successful["last_success_at"] == 120.0
    assert successful["summary"]["created"] == 1
    assert "retry_after_until" not in successful
    assert "retry_after" not in successful

    third, command = _adapter("Changed during cooldown")
    clock["now"] = 130.0
    await third._run_post_connect_initialization()
    third._client.tree.fetch_commands.assert_not_awaited()
    third._client.http.upsert_global_command.assert_not_awaited()

    command.description = "Changed after successful sync"
    assert third._desired_command_sync_fingerprint() != changed_fingerprint
    await third._run_post_connect_initialization()
    third._client.tree.fetch_commands.assert_awaited_once()
    third._client.http.upsert_global_command.assert_awaited_once()
    assert json.loads(state_path.read_bytes())["999"]["last_success_at"] == 130.0
