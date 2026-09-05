"""Fork patch P11 (hq/v2): restart-free Slack bot-token reload.

The Socket Mode connection authenticates with the NON-rotating app token, so a
rotated bot token only invalidates the Web API clients. These tests pin that the
adapter swaps the rotated token onto its live Web clients IN PLACE — no socket
teardown, no restart — and that the whole path is inert unless the
``bot_token_reload_enabled`` flag is on (default-off ⇒ stock behavior).
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler",
         slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402


class _FakeClient:
    """Minimal stand-in for AsyncWebClient: a mutable token + async auth/close."""

    def __init__(self, token, team_id="T_PRIMARY", user_id="U_BOT", user="botname"):
        self.token = token
        self._team_id = team_id
        self._user_id = user_id
        self._user = user
        self.closed = False

    async def auth_test(self):
        return {"team_id": self._team_id, "user_id": self._user_id, "user": self._user}

    async def close(self):
        self.closed = True


def _make_adapter(*, reload_enabled, primary_token="xoxb-old"):
    cfg = PlatformConfig(
        enabled=True,
        token=primary_token,
        bot_token_reload_enabled=reload_enabled,
    )
    adapter = SlackAdapter(cfg)
    app = MagicMock()
    app.client = _FakeClient(primary_token)
    adapter._app = app
    adapter._running = True
    adapter._loaded_bot_token_raw = primary_token
    adapter._team_clients = {"T_PRIMARY": _FakeClient(primary_token)}
    adapter._team_bot_user_ids = {"T_PRIMARY": "U_BOT"}
    adapter._team_bot_names = {"T_PRIMARY": "botname"}
    return adapter


def _write_token_file(tmp_path, value, monkeypatch, *, sentinel=True):
    token_file = tmp_path / "slack-bot-token"
    token_file.write_text(value + "\n", encoding="utf-8")
    monkeypatch.setenv("SLACK_BOT_TOKEN_FILE", str(token_file))
    if sentinel:
        sentinel_file = tmp_path / "slack-token-reloaded"
        sentinel_file.write_text("1", encoding="utf-8")
        monkeypatch.setenv("SLACK_BOT_TOKEN_RELOAD_SENTINEL", str(sentinel_file))
        return token_file, sentinel_file
    return token_file, None


# ── config plumbing ─────────────────────────────────────────────────────────

def test_config_flag_defaults_off_and_round_trips():
    assert PlatformConfig().bot_token_reload_enabled is False
    on = PlatformConfig.from_dict({"bot_token_reload_enabled": True})
    assert on.bot_token_reload_enabled is True
    assert on.to_dict()["bot_token_reload_enabled"] is True
    # bridged-into-extra form is honored too
    via_extra = PlatformConfig.from_dict({"extra": {"bot_token_reload_enabled": True}})
    assert via_extra.bot_token_reload_enabled is True
    # absent ⇒ stock default
    assert PlatformConfig.from_dict({}).bot_token_reload_enabled is False


# ── flag OFF: stock behavior, byte-for-byte no-op ────────────────────────────

@pytest.mark.asyncio
async def test_reload_noop_when_flag_off(tmp_path, monkeypatch):
    _write_token_file(tmp_path, "xoxb-new", monkeypatch)
    adapter = _make_adapter(reload_enabled=False)
    changed = await adapter._reload_bot_token(reason="test")
    assert changed is False
    assert adapter._app.client.token == "xoxb-old"
    assert adapter._team_clients["T_PRIMARY"].token == "xoxb-old"
    assert adapter.config.token == "xoxb-old"


@pytest.mark.asyncio
async def test_watchdog_hook_noop_when_flag_off(tmp_path, monkeypatch):
    _write_token_file(tmp_path, "xoxb-new", monkeypatch)
    adapter = _make_adapter(reload_enabled=False)
    adapter._pending_auth_reload = True  # even with a reactive flag set
    await adapter._maybe_reload_bot_token_from_watchdog()
    assert adapter._app.client.token == "xoxb-old"


def test_auth_error_does_not_arm_reload_when_flag_off():
    adapter = _make_adapter(reload_enabled=False)
    resp = {"error": "invalid_auth"}
    adapter._describe_slack_api_error(resp)
    assert adapter._pending_auth_reload is False


# ── flag ON: in-place rotation, socket preserved ─────────────────────────────

@pytest.mark.asyncio
async def test_same_workspace_rotation_swaps_in_place(tmp_path, monkeypatch):
    _write_token_file(tmp_path, "xoxb-new", monkeypatch)
    adapter = _make_adapter(reload_enabled=True)
    original_app_client = adapter._app.client
    original_team_client = adapter._team_clients["T_PRIMARY"]

    changed = await adapter._reload_bot_token(reason="test")

    assert changed is True
    # Same objects, mutated token — no client rebuild ⇒ in-flight sends survive.
    assert adapter._app.client is original_app_client
    assert adapter._team_clients["T_PRIMARY"] is original_team_client
    assert adapter._app.client.token == "xoxb-new"
    assert adapter._team_clients["T_PRIMARY"].token == "xoxb-new"
    assert adapter.config.token == "xoxb-new"
    assert adapter._loaded_bot_token_raw == "xoxb-new"
    # Socket Mode was never torn down by the reload path.
    assert original_team_client.closed is False


@pytest.mark.asyncio
async def test_unchanged_token_is_noop(tmp_path, monkeypatch):
    _write_token_file(tmp_path, "xoxb-old", monkeypatch)
    adapter = _make_adapter(reload_enabled=True)
    changed = await adapter._reload_bot_token(reason="test")
    assert changed is False
    assert adapter._app.client.token == "xoxb-old"


@pytest.mark.asyncio
async def test_watchdog_reloads_on_sentinel_change(tmp_path, monkeypatch):
    _write_token_file(tmp_path, "xoxb-old", monkeypatch)
    adapter = _make_adapter(reload_enabled=True)
    adapter._bot_token_reload_baseline = adapter._bot_token_reload_signature()

    # Rotate + bump the sentinel signature.
    token_file, sentinel_file = _write_token_file(tmp_path, "xoxb-new", monkeypatch)
    import os
    os.utime(sentinel_file, (10**9, 10**9))

    await adapter._maybe_reload_bot_token_from_watchdog()
    assert adapter._app.client.token == "xoxb-new"


@pytest.mark.asyncio
async def test_watchdog_reloads_on_reactive_auth_flag(tmp_path, monkeypatch):
    _write_token_file(tmp_path, "xoxb-new", monkeypatch, sentinel=False)
    adapter = _make_adapter(reload_enabled=True)
    # No sentinel change; the reactive auth flag alone must drive the reload.
    adapter._bot_token_reload_baseline = adapter._bot_token_reload_signature()
    adapter._pending_auth_reload = True
    await adapter._maybe_reload_bot_token_from_watchdog()
    assert adapter._app.client.token == "xoxb-new"
    assert adapter._pending_auth_reload is False


def test_auth_error_arms_reload_when_flag_on():
    adapter = _make_adapter(reload_enabled=True)
    adapter._describe_slack_api_error({"error": "token_revoked"})
    assert adapter._pending_auth_reload is True
    # A non-auth error must not arm it.
    adapter._pending_auth_reload = False
    adapter._describe_slack_api_error({"error": "missing_scope", "needed": "files:read"})
    assert adapter._pending_auth_reload is False


@pytest.mark.asyncio
async def test_new_source_token_mints_and_maps_client(tmp_path, monkeypatch):
    # A rotation that ALSO introduces a second workspace token (comma list):
    # the new token has no existing client, so auth_test maps its workspace.
    _write_token_file(tmp_path, "xoxb-new1,xoxb-new2", monkeypatch)
    adapter = _make_adapter(reload_enabled=True, primary_token="xoxb-new1")
    # Only the first workspace is currently connected.
    adapter._loaded_bot_token_raw = "xoxb-new1"

    minted = _FakeClient("xoxb-new2", team_id="T_SECOND", user_id="U2", user="bot2")
    monkeypatch.setattr(_slack_mod, "AsyncWebClient", lambda **kw: minted)
    monkeypatch.setattr(_slack_mod, "_apply_slack_proxy", lambda *a, **k: None)

    changed = await adapter._reload_bot_token(reason="test")
    assert changed is True
    assert adapter._team_clients["T_SECOND"] is minted
    assert adapter._team_bot_user_ids["T_SECOND"] == "U2"
    # The already-connected workspace is left untouched.
    assert "T_PRIMARY" in adapter._team_clients
