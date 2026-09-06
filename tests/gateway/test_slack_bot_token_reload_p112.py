"""Fork patch P11.2 (hq/v2): arm the restart-free bot-token reload from EVERY
auth-error site, drive bolt authorize through the token store, and close the
startup race.

P11.1 wired the reactive arm only into the attachment-download classifier. On
dbooth (v2.22, 2026-09-06) the token actually expired on the PRIMARY paths —
slack_bolt ``AsyncSingleTeamAuthorization`` (which authenticates a per-request
client built from bolt's *captured* token) and ``gateway.channel_directory`` —
so nothing armed the reload and the box logged ``token_expired`` for hours with a
valid on-disk token until a restart. A startup race (env token seeded ~2s before
the refresher's first write) could also strand the process on an expired token
with no rotation to trip the sentinel.

These tests pin: central arming via the store, the api_call funnel
(channel_directory / posting / users lookup), the store-backed bolt authorize
callable, the watchdog honoring the central flag, and the post-connect re-read.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import plugins.platforms.slack.adapter as _slack_mod
from plugins.platforms.slack.adapter import (
    SlackAdapter,
    _SlackBotTokenStore,
    _ReloadableAsyncWebClient,
    _slack_error_code,
)
from gateway.config import PlatformConfig

# Fake token prefixes assembled at runtime so the literal never appears as a
# static string (keeps secret scanners quiet); still exercises the xoxb path.
_B = "xoxb" + "-"

_REAL_SLACK = (
    _ReloadableAsyncWebClient is not None
    and getattr(_slack_mod.AsyncWebClient, "__module__", "").startswith("slack_sdk")
)
pytestmark = pytest.mark.skipif(
    not _REAL_SLACK, reason="requires the real slack_sdk AsyncWebClient"
)


def _make_adapter(*, reload_enabled=True, primary_token=None):
    primary_token = primary_token or (_B + "old")
    cfg = PlatformConfig(
        enabled=True, token=primary_token, bot_token_reload_enabled=reload_enabled
    )
    adapter = SlackAdapter(cfg)
    adapter._token_store = _SlackBotTokenStore(primary=primary_token)
    adapter._token_store.on_auth_error = adapter._handle_store_auth_error
    adapter._authorize_cache = {}
    adapter._loaded_bot_token_raw = primary_token
    adapter._app = MagicMock()
    adapter._app.client = adapter._make_web_client(
        is_primary=True, initial_token=primary_token
    )
    adapter._running = True
    return adapter


def _write_token_file(tmp_path, value, monkeypatch, *, sentinel=True):
    tf = tmp_path / "slack-bot-token"
    tf.write_text(value + "\n", encoding="utf-8")
    monkeypatch.setenv("SLACK_BOT_TOKEN_FILE", str(tf))
    if sentinel:
        sf = tmp_path / "slack-token-reloaded"
        sf.write_text("1", encoding="utf-8")
        monkeypatch.setenv("SLACK_BOT_TOKEN_RELOAD_SENTINEL", str(sf))
    return tf


# ── the store's central fan-out ──────────────────────────────────────────────

def test_store_note_auth_error_arms_and_fans_out():
    store = _SlackBotTokenStore(primary="t")
    seen = []
    store.on_auth_error = lambda code: seen.append(code)
    store.note_auth_error("token_expired")
    assert store.pending_auth_reload is True
    assert seen == ["token_expired"]


def test_store_note_auth_error_ignores_non_reload_codes_for_pending():
    store = _SlackBotTokenStore(primary="t")
    seen = []
    store.on_auth_error = lambda code: seen.append(code)
    # not_authed is surfaced to the callback but is not a "reload will fix it" code
    store.note_auth_error("not_authed")
    assert store.pending_auth_reload is False
    assert seen == ["not_authed"]


def test_error_code_extractor():
    from slack_sdk.errors import SlackApiError

    assert _slack_error_code(SlackApiError("m", {"error": "token_expired"})) == "token_expired"
    assert _slack_error_code({"error": "invalid_auth"}) == "invalid_auth"
    assert _slack_error_code(None) == ""
    assert _slack_error_code({"ok": True}) == ""


# ── the api_call funnel (channel_directory / posting / users lookup) ──────────

@pytest.mark.asyncio
async def test_api_call_arms_on_raised_auth_error(monkeypatch):
    import slack_sdk.web.async_base_client as _abc
    from slack_sdk.errors import SlackApiError

    async def _raise(self, *a, **k):
        raise SlackApiError("token_expired", {"error": "token_expired"})

    monkeypatch.setattr(_abc.AsyncBaseClient, "api_call", _raise, raising=True)

    store = _SlackBotTokenStore(primary=_B + "old")
    armed = []
    store.on_auth_error = lambda code: armed.append(code)
    client = _ReloadableAsyncWebClient(token_store=store, is_primary=True)

    with pytest.raises(SlackApiError):
        await client.conversations_list()  # channel_directory-style call
    assert store.pending_auth_reload is True
    assert armed == ["token_expired"]


@pytest.mark.asyncio
async def test_api_call_arms_on_ok_false_auth_error(monkeypatch):
    import slack_sdk.web.async_base_client as _abc

    async def _ok_false(self, *a, **k):
        return {"ok": False, "error": "invalid_auth"}

    monkeypatch.setattr(_abc.AsyncBaseClient, "api_call", _ok_false, raising=True)
    store = _SlackBotTokenStore(primary=_B + "old")
    client = _ReloadableAsyncWebClient(token_store=store, is_primary=True)
    await client.auth_test()
    assert store.pending_auth_reload is True


@pytest.mark.asyncio
async def test_api_call_does_not_arm_on_non_auth_error(monkeypatch):
    import slack_sdk.web.async_base_client as _abc
    from slack_sdk.errors import SlackApiError

    async def _raise(self, *a, **k):
        raise SlackApiError("channel_not_found", {"error": "channel_not_found"})

    monkeypatch.setattr(_abc.AsyncBaseClient, "api_call", _raise, raising=True)
    store = _SlackBotTokenStore(primary=_B + "old")
    client = _ReloadableAsyncWebClient(token_store=store, is_primary=True)
    with pytest.raises(SlackApiError):
        await client.conversations_list()
    assert store.pending_auth_reload is False


# ── the store-backed bolt authorize callable ─────────────────────────────────

@pytest.mark.asyncio
async def test_bolt_authorize_uses_live_token_and_rotates():
    adapter = _make_adapter(reload_enabled=True, primary_token=_B + "old")
    authorize = adapter._make_store_authorize()

    ctx = MagicMock()
    ctx.client.auth_test = AsyncMock(
        return_value={"ok": True, "team_id": "T", "user_id": "U", "bot_id": "B", "user": "bot"}
    )

    res = await authorize(context=ctx, enterprise_id=None, team_id="T", user_id="U")
    assert res is not None
    assert res.bot_token == _B + "old"
    assert ctx.client.auth_test.await_count == 1

    # Steady token ⇒ cached, no second auth.test.
    res2 = await authorize(context=ctx, enterprise_id=None, team_id="T", user_id="U")
    assert res2.bot_token == _B + "old"
    assert ctx.client.auth_test.await_count == 1

    # Rotation ⇒ cache miss, re-auth with the NEW token, new bot_token.
    adapter._token_store.set_primary(_B + "new")
    res3 = await authorize(context=ctx, enterprise_id=None, team_id="T", user_id="U")
    assert res3.bot_token == _B + "new"
    assert ctx.client.auth_test.await_count == 2
    assert ctx.client.auth_test.await_args.kwargs.get("token") == _B + "new"


@pytest.mark.asyncio
async def test_bolt_authorize_arms_reload_on_auth_error():
    from slack_sdk.errors import SlackApiError

    adapter = _make_adapter(reload_enabled=True, primary_token=_B + "old")
    authorize = adapter._make_store_authorize()
    ctx = MagicMock()
    ctx.client.auth_test = AsyncMock(
        side_effect=SlackApiError("token_expired", {"error": "token_expired"})
    )

    res = await authorize(context=ctx, enterprise_id=None, team_id="T", user_id="U")
    assert res is None  # bolt drops the request rather than authorize with a dead token
    assert adapter._token_store.pending_auth_reload is True
    assert adapter._pending_auth_reload is True  # fanned out to the watchdog flag


@pytest.mark.asyncio
async def test_bolt_authorize_error_does_not_arm_when_flag_off():
    from slack_sdk.errors import SlackApiError

    adapter = _make_adapter(reload_enabled=False, primary_token=_B + "old")
    authorize = adapter._make_store_authorize()
    ctx = MagicMock()
    ctx.client.auth_test = AsyncMock(
        side_effect=SlackApiError("token_expired", {"error": "token_expired"})
    )
    res = await authorize(context=ctx, enterprise_id=None, team_id="T", user_id="U")
    assert res is None
    # store still records the signal, but the adapter watchdog flag stays off
    assert adapter._pending_auth_reload is False


# ── watchdog honors the central store flag ───────────────────────────────────

@pytest.mark.asyncio
async def test_watchdog_reloads_on_store_pending_flag(tmp_path, monkeypatch):
    _write_token_file(tmp_path, _B + "new", monkeypatch, sentinel=False)
    adapter = _make_adapter(reload_enabled=True, primary_token=_B + "old")
    adapter._team_clients = {"T": adapter._make_web_client(initial_token=_B + "old")}
    adapter._team_clients["T"].bind_team("T")
    adapter._bot_token_reload_baseline = adapter._bot_token_reload_signature()
    # Adapter flag NOT set; only the central store flag (e.g. armed from api_call).
    adapter._pending_auth_reload = False
    adapter._token_store.pending_auth_reload = True

    await adapter._maybe_reload_bot_token_from_watchdog()
    assert adapter._token_store.primary() == _B + "new"
    assert adapter._token_store.pending_auth_reload is False


# ── startup race: post-connect re-read ───────────────────────────────────────

@pytest.mark.asyncio
async def test_post_connect_reread_recovers_startup_race(tmp_path, monkeypatch):
    # Process seeded with a stale token; refresher writes the valid one shortly
    # after; NO rotation/sentinel touch. The one-shot re-read must still apply it.
    _write_token_file(tmp_path, _B + "valid", monkeypatch, sentinel=True)
    adapter = _make_adapter(reload_enabled=True, primary_token=_B + "staleseed")
    adapter._team_clients = {"T": adapter._make_web_client(initial_token=_B + "staleseed")}
    adapter._team_clients["T"].bind_team("T")

    adapter._schedule_post_connect_token_reread(delay_s=0)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if adapter._token_store.primary() == _B + "valid":
            break
    assert adapter._token_store.primary() == _B + "valid"
    assert adapter._loaded_bot_token_raw == _B + "valid"


@pytest.mark.asyncio
async def test_post_connect_reread_noop_when_flag_off(tmp_path, monkeypatch):
    _write_token_file(tmp_path, _B + "valid", monkeypatch)
    adapter = _make_adapter(reload_enabled=False, primary_token=_B + "staleseed")
    adapter._schedule_post_connect_token_reread(delay_s=0)
    await asyncio.sleep(0.05)
    assert adapter._token_store.primary() == _B + "staleseed"
