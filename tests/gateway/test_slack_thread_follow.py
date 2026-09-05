"""
Tests for Slack persisted thread-follow (Fork patch P12, hq/v2).

The stock in-memory wake checks (``_bot_message_ts`` / ``_mentioned_threads``)
are wiped on every gateway restart, so an un-mentioned follow-up in a thread
the bot already replied in can be silently dropped after a restart. When
``thread_follow_replies`` is enabled, the adapter also records bot-participated
threads to a bounded LRU persisted under ``HERMES_HOME`` (see
``_record_followed_thread`` / ``_is_followed_thread`` /
``_should_wake_on_unmentioned_message`` in
``plugins/platforms/slack/adapter.py``), so the follow-up is still recognised
as addressed after restart. Default is OFF (stock behaviour unchanged).

Follows the same mocking/adapter-construction pattern as
test_slack_mention.py / test_slack_thread_require_mention.py.
"""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Mock slack-bolt if not installed (same pattern as test_slack_mention.py)
# ---------------------------------------------------------------------------

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
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod
_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402
from gateway.config import Platform, PlatformConfig  # noqa: E402


def run(coro):
    return asyncio.run(coro)


TEAM_ID = "T1"
CHANNEL_ID = "C123"
USER_ID = "U999"
THREAD_TS = "1700000000.100200"


def _make_adapter(thread_follow_replies=None, hermes_home=None, monkeypatch=None):
    extra = {}
    if thread_follow_replies is not None:
        extra["thread_follow_replies"] = thread_follow_replies

    if hermes_home is not None and monkeypatch is not None:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._bot_user_id = "U_BOT"
    adapter._team_bot_user_ids = {}
    adapter._bot_message_ts = set()
    adapter._BOT_TS_MAX = 5000
    adapter._mentioned_threads = set()
    adapter._MENTIONED_THREADS_MAX = 5000
    adapter._has_active_session_for_thread = lambda **_: False

    async def _no_bot_authored(**_):
        return False

    async def _no_parent_text(**_):
        return ""

    adapter._bot_authored_thread_root = _no_bot_authored
    adapter._fetch_thread_parent_text = _no_parent_text

    # Fork patch P12: initialize the persisted-follow LRU exactly as
    # SlackAdapter.__init__ would (loads from HERMES_HOME if present).
    from collections import OrderedDict

    adapter._followed_threads = OrderedDict()
    adapter._FOLLOWED_THREADS_MAX = 5000
    adapter._load_followed_threads()

    return adapter


async def _wake(adapter, thread_ts=THREAD_TS, is_thread_reply=True, team_id=TEAM_ID):
    return await adapter._should_wake_on_unmentioned_message(
        event_thread_ts=thread_ts,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        is_thread_reply=is_thread_reply,
        team_id=team_id,
    )


# ---------------------------------------------------------------------------
# Tests: _slack_thread_follow_replies accessor
# ---------------------------------------------------------------------------


def test_thread_follow_replies_defaults_to_false(monkeypatch):
    monkeypatch.delenv("SLACK_THREAD_FOLLOW_REPLIES", raising=False)
    adapter = _make_adapter()
    assert adapter._slack_thread_follow_replies() is False


def test_thread_follow_replies_env_var_fallback(monkeypatch):
    monkeypatch.setenv("SLACK_THREAD_FOLLOW_REPLIES", "true")
    adapter = _make_adapter()
    assert adapter._slack_thread_follow_replies() is True


def test_thread_follow_replies_config_true():
    adapter = _make_adapter(thread_follow_replies=True)
    assert adapter._slack_thread_follow_replies() is True


def test_thread_follow_replies_config_string_false():
    adapter = _make_adapter(thread_follow_replies="false")
    assert adapter._slack_thread_follow_replies() is False


# ---------------------------------------------------------------------------
# Scenario 1: flag ON, bot replied in the thread → wakes on un-mentioned reply
# ---------------------------------------------------------------------------


def test_flag_on_wakes_on_followed_thread(monkeypatch, tmp_path):
    adapter = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    # Simulate the bot having replied into the thread (the same call site
    # invoked from the send() / file-upload paths).
    adapter._record_followed_thread(TEAM_ID, THREAD_TS)

    assert run(_wake(adapter)) is True


# ---------------------------------------------------------------------------
# Scenario 2: flag ON, persistence survives a fresh adapter instance
# (simulating a gateway restart which wipes _bot_message_ts / _mentioned_threads)
# ---------------------------------------------------------------------------


def test_flag_on_persists_across_restart(monkeypatch, tmp_path):
    adapter1 = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    adapter1._record_followed_thread(TEAM_ID, THREAD_TS)

    # A brand-new adapter instance pointed at the same HERMES_HOME — mirrors
    # a gateway restart. Its in-memory sets are empty, but the persisted LRU
    # is reloaded from disk.
    adapter2 = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    assert adapter2._bot_message_ts == set()
    assert adapter2._mentioned_threads == set()

    assert run(_wake(adapter2)) is True


# ---------------------------------------------------------------------------
# Scenario 3: flag ON, never-joined thread → ignored
# ---------------------------------------------------------------------------


def test_flag_on_never_joined_thread_ignored(monkeypatch, tmp_path):
    adapter = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    assert run(_wake(adapter, thread_ts="1700000000.999999")) is False


# ---------------------------------------------------------------------------
# Scenario 4: flag OFF → the persisted-follow branch is inert
# ---------------------------------------------------------------------------


def test_flag_off_persisted_follow_branch_is_inert(monkeypatch, tmp_path):
    adapter = _make_adapter(
        thread_follow_replies=False, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    # Seed ONLY the persisted-follow store (not the legacy in-memory sets) —
    # simulates a followed thread recorded while the flag was on, then the
    # flag being turned back off (a dropped patch on a pin bump).
    adapter._followed_threads[adapter._thread_follow_key(TEAM_ID, THREAD_TS)] = 0.0
    adapter._followed_threads[str(THREAD_TS)] = 0.0

    assert run(_wake(adapter)) is False


def test_flag_off_does_not_record_followed_thread(monkeypatch, tmp_path):
    """_record_followed_thread() is a no-op when the flag is off."""
    adapter = _make_adapter(
        thread_follow_replies=False, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    adapter._record_followed_thread(TEAM_ID, THREAD_TS)
    assert adapter._followed_threads == {}


def test_flag_off_stock_wake_checks_unaffected(monkeypatch, tmp_path):
    """Existing in-memory wake checks (_bot_message_ts) still work when the
    new flag is off — P12 must not regress stock behaviour."""
    adapter = _make_adapter(
        thread_follow_replies=False, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    adapter._bot_message_ts.add(adapter._workspace_message_marker(TEAM_ID, THREAD_TS))

    assert run(_wake(adapter)) is True


def test_flag_off_unmentioned_never_joined_thread_ignored(monkeypatch, tmp_path):
    adapter = _make_adapter(
        thread_follow_replies=False, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    assert run(_wake(adapter)) is False


# ---------------------------------------------------------------------------
# Tests: persistence is crash-safe (atomic) + LRU bound + new-thread mention
# ---------------------------------------------------------------------------


def test_persist_is_atomic_no_temp_left(monkeypatch, tmp_path):
    """A successful persist leaves a valid JSON file and no sibling temp."""
    import json as _json

    adapter = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    adapter._record_followed_thread(TEAM_ID, THREAD_TS)

    path = adapter._followed_threads_path()
    assert path is not None and path.exists()
    data = _json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data  # non-empty key list
    # No leftover atomic-write temp files in the directory.
    leftovers = [p.name for p in path.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == [], f"atomic-write temp not cleaned: {leftovers}"


def test_persist_torn_write_leaves_prior_file_intact(monkeypatch, tmp_path):
    """If os.replace fails mid-persist, the previously-persisted file is not
    corrupted and no temp file is left behind (crash-safety of the atomic path)."""
    import json as _json

    adapter = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    # First, a good persist establishes a valid on-disk file.
    adapter._record_followed_thread(TEAM_ID, THREAD_TS)
    path = adapter._followed_threads_path()
    good = path.read_text(encoding="utf-8")

    # Now force the replace to fail on the next persist.
    def _boom(*_a, **_k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(_slack_mod.os, "replace", _boom)
    adapter._record_followed_thread("T_OTHER", "222.333")  # triggers a persist

    # Original file is byte-identical (never partially overwritten) ...
    assert path.read_text(encoding="utf-8") == good
    # ... it is still valid JSON ...
    assert isinstance(_json.loads(path.read_text(encoding="utf-8")), list)
    # ... and the temp file was cleaned up despite the failure.
    leftovers = [p.name for p in path.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == [], f"atomic-write temp not cleaned on failure: {leftovers}"


def test_lru_is_bounded_and_evicts_oldest(monkeypatch, tmp_path):
    """The followed-threads LRU never grows past _FOLLOWED_THREADS_MAX."""
    adapter = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    adapter._FOLLOWED_THREADS_MAX = 10  # small bound for the test
    for i in range(50):
        adapter._record_followed_thread(TEAM_ID, f"ts-{i}")
    assert len(adapter._followed_threads) <= adapter._FOLLOWED_THREADS_MAX
    # The oldest thread must have been evicted; the newest must be present.
    assert not adapter._is_followed_thread(TEAM_ID, "ts-0")
    assert adapter._is_followed_thread(TEAM_ID, "ts-49")


def test_flag_on_new_thread_still_requires_mention(monkeypatch, tmp_path):
    """A brand-new thread with no follow record does not wake on an
    un-mentioned message even with the flag on (is_thread_reply=False)."""
    adapter = _make_adapter(
        thread_follow_replies=True, hermes_home=tmp_path, monkeypatch=monkeypatch
    )
    assert run(_wake(adapter, thread_ts="999.000", is_thread_reply=False)) is False
