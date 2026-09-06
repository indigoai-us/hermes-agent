"""Fork patch P11.1 (hq/v2): single-source bot-token provider.

P11 swapped the rotated token onto the two client collections it knew about
(``app.client`` + ``_team_clients``). Any *other* Web client the gateway held —
the channel_directory sweep, thread-follow, file upload, users lookup — that
captured the startup token kept using it after a rotation, so those calls failed
with ``token_expired`` until a full restart while ``gateway_state`` still said
Slack was connected.

P11.1 makes a single ``_SlackBotTokenStore`` the ONE source of truth: every Web
client reads its token from the store at call time, so a rotation is one store
update that every client — even one the reload path never touches — observes
atomically. These tests pin that invariant, the ``token_expired`` reactive
trigger, the degraded/connected gateway-status transition, and a source-level
tripwire that no live-adapter client is ever built from a captured token string.
"""

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import plugins.platforms.slack.adapter as _slack_mod
from plugins.platforms.slack.adapter import (
    SlackAdapter,
    _SlackBotTokenStore,
    _ReloadableAsyncWebClient,
)
from gateway.config import PlatformConfig

# The provider tests exercise the REAL slack_sdk subclass. When slack_sdk is
# only present as a MagicMock (some isolated import orders), the subclass has a
# mock base and cannot be instantiated meaningfully — skip rather than assert
# against a mock. In CI (slack extra installed) this never skips.
_REAL_SLACK = (
    _ReloadableAsyncWebClient is not None
    and getattr(_slack_mod.AsyncWebClient, "__module__", "").startswith("slack_sdk")
)

pytestmark = pytest.mark.skipif(
    not _REAL_SLACK, reason="requires the real slack_sdk AsyncWebClient"
)


def _make_adapter(*, reload_enabled=True, primary_token="xoxb-old"):
    cfg = PlatformConfig(
        enabled=True,
        token=primary_token,
        bot_token_reload_enabled=reload_enabled,
    )
    return SlackAdapter(cfg)


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


# ── the store itself ─────────────────────────────────────────────────────────

def test_store_primary_and_team_resolution():
    store = _SlackBotTokenStore(primary="p0")
    assert store.primary() == "p0"
    # Unknown team falls back to primary.
    assert store.get("T_UNKNOWN") == "p0"
    store.set_team("T1", "t1")
    assert store.get("T1") == "t1"
    # A rotation replaces every slot and bumps the generation.
    gen = store.generation
    store.rotate(primary="p1", by_team={"T1": "t1b"})
    assert store.primary() == "p1"
    assert store.get("T1") == "t1b"
    assert store.generation == gen + 1


def test_reloadable_client_reads_token_live_from_store():
    store = _SlackBotTokenStore(primary="xoxb-old")
    primary = _ReloadableAsyncWebClient(token_store=store, is_primary=True)
    team = _ReloadableAsyncWebClient(token_store=store, initial_token="xoxb-old")
    team.bind_team("T1")

    assert primary.token == "xoxb-old"
    assert team.token == "xoxb-old"

    # One store update ⇒ both clients see the new value with no per-client swap.
    store.set_primary("xoxb-new")
    store.set_team("T1", "xoxb-new")
    assert primary.token == "xoxb-new"
    assert team.token == "xoxb-new"


# ── the core invariant: an untouched client still sees the rotated token ──────

def _wire_adapter_clients(adapter, token):
    """Give the adapter a primary app client + one registered team client, and
    return an extra 'helper' client that shares the store but is deliberately
    NOT registered in ``_team_clients`` (it stands in for channel_directory /
    file-upload / users-lookup clients captured at startup)."""
    adapter._token_store = _SlackBotTokenStore(primary=token)
    # Mirror connect(): the store fans auth errors back to the adapter.
    adapter._token_store.on_auth_error = adapter._handle_store_auth_error
    adapter._authorize_cache = {}
    adapter._slack_auth_degraded = False

    app = MagicMock()
    app.client = adapter._make_web_client(is_primary=True)
    adapter._app = app
    adapter._running = True
    adapter._loaded_bot_token_raw = token

    team = adapter._make_web_client(initial_token=token)
    team.bind_team("T_PRIMARY")
    adapter._team_clients = {"T_PRIMARY": team}
    adapter._team_bot_user_ids = {"T_PRIMARY": "U_BOT"}
    adapter._team_bot_names = {"T_PRIMARY": "botname"}

    helper = adapter._make_web_client(initial_token=token)
    helper.bind_team("T_PRIMARY")
    return app.client, team, helper


@pytest.mark.asyncio
async def test_rotation_refreshes_every_client_including_untracked(tmp_path, monkeypatch):
    adapter = _make_adapter(reload_enabled=True)
    app_client, team_client, helper_client = _wire_adapter_clients(adapter, "xoxb-old")
    monkeypatch.setattr(_slack_mod, "_apply_slack_proxy", lambda *a, **k: None)

    _write_token_file(tmp_path, "xoxb-new", monkeypatch)
    changed = await adapter._reload_bot_token(reason="test")

    assert changed is True
    # Reload only ever touched app.client + _team_clients …
    assert app_client.token == "xoxb-new"
    assert team_client.token == "xoxb-new"
    # … but the helper client, which the reload path NEVER references, sees the
    # new token too, because it reads from the same store. This is the P11.1 fix.
    assert helper_client.token == "xoxb-new"
    assert adapter._token_store.primary() == "xoxb-new"


@pytest.mark.asyncio
async def test_post_after_rotation_uses_new_token(tmp_path, monkeypatch):
    adapter = _make_adapter(reload_enabled=True)
    _app_client, team_client, helper_client = _wire_adapter_clients(adapter, "xoxb-old")
    monkeypatch.setattr(_slack_mod, "_apply_slack_proxy", lambda *a, **k: None)

    # Capture the token slack_sdk would actually send (api_call reads self.token).
    recorded = {}

    async def _fake_api_call(self, *args, **kwargs):
        recorded["token"] = self.token
        return {"ok": True}

    monkeypatch.setattr(
        _ReloadableAsyncWebClient, "api_call", _fake_api_call, raising=False
    )

    _write_token_file(tmp_path, "xoxb-new", monkeypatch)
    assert await adapter._reload_bot_token(reason="test") is True

    # A post through the helper (never touched by the reload) carries the NEW
    # token, so it would succeed instead of 401ing with token_expired.
    resp = await helper_client.chat_postMessage(channel="C1", text="hi")
    assert resp["ok"] is True
    assert recorded["token"] == "xoxb-new"

    recorded.clear()
    await team_client.chat_postMessage(channel="C1", text="hi")
    assert recorded["token"] == "xoxb-new"


# ── token_expired: reactive trigger + degraded/connected status transition ────

def test_token_expired_arms_reload_when_flag_on():
    adapter = _make_adapter(reload_enabled=True)
    adapter._describe_slack_api_error({"error": "token_expired"})
    assert adapter._pending_auth_reload is True


def test_token_expired_does_not_arm_reload_when_flag_off():
    adapter = _make_adapter(reload_enabled=False)
    adapter._describe_slack_api_error({"error": "token_expired"})
    assert adapter._pending_auth_reload is False


@pytest.mark.asyncio
async def test_auth_failure_reports_degraded_then_recovers(tmp_path, monkeypatch):
    import gateway.status as _status_mod

    writes = []
    monkeypatch.setattr(
        _status_mod, "write_runtime_status", lambda **kw: writes.append(kw)
    )

    adapter = _make_adapter(reload_enabled=True)
    _wire_adapter_clients(adapter, "xoxb-old")
    monkeypatch.setattr(_slack_mod, "_apply_slack_proxy", lambda *a, **k: None)

    # A live Web call returns token_expired ⇒ gateway marked degraded.
    adapter._describe_slack_api_error({"error": "token_expired"})
    degraded = [w for w in writes if w.get("platform_state") == "degraded"]
    assert degraded, "expected a degraded runtime-status write"
    assert degraded[-1]["error_code"] == "token_expired"
    assert degraded[-1].get("needs_attention") is True

    # A second identical failure must NOT re-write (transition-only, no flap).
    before = len(writes)
    adapter._describe_slack_api_error({"error": "token_expired"})
    assert len(writes) == before

    # A successful reload clears the degrade back to connected.
    _write_token_file(tmp_path, "xoxb-new", monkeypatch)
    assert await adapter._reload_bot_token(reason="test") is True
    recovered = [w for w in writes if w.get("platform_state") == "connected"]
    assert recovered, "expected a connected runtime-status write after reload"
    assert adapter._slack_auth_degraded is False


# ── tripwire: no live-adapter client is built from a captured token string ────

def test_tripwire_no_captured_token_web_client_instantiations():
    """Every ``AsyncWebClient`` / ``_ReloadableAsyncWebClient`` construction must
    live in an allow-listed factory. New live-adapter code that mints a client
    from a captured token string (the class of bug P11.1 fixes) fails here.

    Allowed:
      * ``_make_web_client``      — the single store-bound factory.
      * ``_standalone_send`` /
        ``_standalone_upload_file`` — out-of-process delivery that re-resolves
        the token from config/secret on every call (never a cached startup
        token), so it is not part of the live adapter's reloadable client set.
    """
    src = Path(_slack_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    allowed = {"_make_web_client", "_standalone_send", "_standalone_upload_file"}

    # Map each node to its nearest enclosing function via parent links.
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_func(node):
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parents.get(cur)
        return None

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name not in {"AsyncWebClient", "_AsyncWebClient", "_ReloadableAsyncWebClient"}:
            continue
        enc = enclosing_func(node)
        if enc not in allowed:
            offenders.append((enc, getattr(node, "lineno", "?")))

    assert not offenders, (
        "Web client constructed outside the store-bound factory "
        f"(captured-token risk): {offenders}. Route it through _make_web_client."
    )


def test_tripwire_make_web_client_is_the_only_reloadable_factory():
    """The reloadable subclass must be instantiated in exactly one place."""
    src = Path(_slack_mod.__file__).read_text(encoding="utf-8")
    count = sum(
        1
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) == "_ReloadableAsyncWebClient")
    )
    assert count == 1, f"expected 1 _ReloadableAsyncWebClient() call, found {count}"
