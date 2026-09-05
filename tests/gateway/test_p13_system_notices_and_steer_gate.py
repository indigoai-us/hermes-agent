"""Fork patch P13 — per-turn system-notice master gate + shared-channel
steer/redirect safety.

Two independent fork changes, both flag-gated so stock upstream behavior is the
default:

1. ``GatewayConfig.system_notices_enabled`` (default True = stock) turns OFF
   every unprompted, system-generated *per-turn* notice the gateway pushes to a
   platform — busy acks ("⏩ Steered", "↪ Redirected current run", "⏳ Queued",
   "⚡ Interrupting") and their appended "💡 First-time tip", the long-tool
   /verbose progress hint, the first-message profile-build offer, and
   background-review deliveries ("💾 Memory updated", "💾 Self-improvement
   review: … User profile updated"). HQ fleet boxes render it false so an agent
   posts ONLY its final reply into a shared channel (incident 2026-09-04; policy
   ``indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages``). Distinct
   from P9, which covers gateway lifecycle (shutdown/restart/online) broadcasts.

2. ``GatewayConfig.steer_requires_same_user_mention`` (default False = stock)
   makes a new inbound message in a SHARED channel/thread splice into the
   running turn (steer / active-turn redirect) only when it is safe: a DM always
   splices; a different user than the one whose request is running never
   splices (queued as its own turn, no ack); a same-user message splices only
   when it @-mentions the agent. Closes the cross-person redirect incident where
   one human's channel message redirected another human's running task.
"""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, _dict_system_notices_enabled
from gateway.session import SessionSource


# ── config plumbing ──────────────────────────────────────────────────────────


def test_config_defaults_reproduce_stock_behavior():
    cfg = GatewayConfig()
    # Per-turn notices ON by default (stock upstream posts them); the steer
    # safety gate OFF by default (stock splices any authorized follow-up).
    assert cfg.system_notices_enabled is True
    assert cfg.steer_requires_same_user_mention is False


def test_config_to_dict_from_dict_round_trip():
    cfg = GatewayConfig(
        system_notices_enabled=False,
        steer_requires_same_user_mention=True,
    )
    data = cfg.to_dict()
    assert data["system_notices_enabled"] is False
    assert data["steer_requires_same_user_mention"] is True
    restored = GatewayConfig.from_dict(data)
    assert restored.system_notices_enabled is False
    assert restored.steer_requires_same_user_mention is True


def test_config_from_dict_coerces_and_defaults():
    # Missing keys fall back to the dataclass defaults.
    restored = GatewayConfig.from_dict({})
    assert restored.system_notices_enabled is True
    assert restored.steer_requires_same_user_mention is False
    # String / truthy coercion mirrors the other gateway bools.
    coerced = GatewayConfig.from_dict(
        {"system_notices_enabled": "false", "steer_requires_same_user_mention": "true"}
    )
    assert coerced.system_notices_enabled is False
    assert coerced.steer_requires_same_user_mention is True


@pytest.mark.parametrize(
    "cfg,expected",
    [
        (None, True),
        ({}, True),
        ({"system_notices_enabled": False}, False),
        ({"system_notices_enabled": "off"}, False),
        ({"gateway": {"system_notices_enabled": False}}, False),
        # Top-level wins over the nested gateway.* fallback.
        (
            {"system_notices_enabled": True, "gateway": {"system_notices_enabled": False}},
            True,
        ),
    ],
)
def test_dict_system_notices_precedence(cfg, expected):
    assert _dict_system_notices_enabled(cfg) is expected


# ── steer/redirect decision matrix (_active_turn_splice_allowed) ──────────────


def _splice_runner(*, steer_gate: bool) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(steer_requires_same_user_mention=steer_gate)
    runner._session_sources = OrderedDict()
    runner._session_sources_max = 512
    runner._cache_session_source = GatewayRunner._cache_session_source.__get__(
        runner, GatewayRunner
    )
    runner._get_cached_session_source = GatewayRunner._get_cached_session_source.__get__(
        runner, GatewayRunner
    )
    runner._active_turn_splice_allowed = (
        GatewayRunner._active_turn_splice_allowed.__get__(runner, GatewayRunner)
    )
    return runner


@dataclass
class _FakeEvent:
    source: SessionSource
    metadata: dict


def _event(chat_type: str, user_id: str, mentioned: bool) -> _FakeEvent:
    src = SessionSource(
        platform=Platform.SLACK,
        chat_id="C-shared",
        chat_type=chat_type,
        user_id=user_id,
        thread_id="t-1",
    )
    return _FakeEvent(source=src, metadata={"hermes_is_mentioned": mentioned})


def _prime_owner(runner: GatewayRunner, owner_user: str) -> str:
    """Cache the running turn's owner source and return the session key."""
    owner = SessionSource(
        platform=Platform.SLACK,
        chat_id="C-shared",
        chat_type="group",
        user_id=owner_user,
        thread_id="t-1",
    )
    session_key = "slack:C-shared:t-1"
    runner._cache_session_source(session_key, owner)
    return session_key


def test_splice_allowed_when_gate_off_is_stock():
    """Flag off ⇒ always splice, regardless of user or mention (stock)."""
    runner = _splice_runner(steer_gate=False)
    session_key = _prime_owner(runner, "alice")
    # A different user with no mention would normally be blocked when the gate
    # is on; with the gate off it splices (unchanged upstream behavior).
    ev = _event("group", "bob", mentioned=False)
    assert runner._active_turn_splice_allowed(ev, session_key) is True


def test_splice_dm_always_allowed_even_with_gate_on():
    runner = _splice_runner(steer_gate=True)
    session_key = _prime_owner(runner, "alice")
    ev = _event("dm", "alice", mentioned=False)
    assert runner._active_turn_splice_allowed(ev, session_key) is True


def test_splice_blocked_for_different_user_in_shared_channel():
    """The incident: a message from a DIFFERENT user must not splice."""
    runner = _splice_runner(steer_gate=True)
    session_key = _prime_owner(runner, "alice")
    ev = _event("group", "bob", mentioned=True)  # even a mention doesn't help
    assert runner._active_turn_splice_allowed(ev, session_key) is False


def test_splice_blocked_for_same_user_without_mention():
    runner = _splice_runner(steer_gate=True)
    session_key = _prime_owner(runner, "alice")
    ev = _event("group", "alice", mentioned=False)
    assert runner._active_turn_splice_allowed(ev, session_key) is False


def test_splice_allowed_for_same_user_mention_in_thread():
    runner = _splice_runner(steer_gate=True)
    session_key = _prime_owner(runner, "alice")
    ev = _event("group", "alice", mentioned=True)
    assert runner._active_turn_splice_allowed(ev, session_key) is True


def test_splice_blocked_when_owner_unknown_in_shared_channel():
    """No cached owner ⇒ cannot prove same-user ⇒ do not splice (conservative)."""
    runner = _splice_runner(steer_gate=True)
    ev = _event("group", "alice", mentioned=True)
    assert runner._active_turn_splice_allowed(ev, "slack:C-shared:t-1") is False


# ── system-notice master gate: predicate on the runner ───────────────────────


def test_runner_system_notices_predicate_reads_config():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(system_notices_enabled=False)
    assert runner._system_notices_enabled() is False
    runner.config = GatewayConfig(system_notices_enabled=True)
    assert runner._system_notices_enabled() is True


def test_runner_system_notices_predicate_fail_open_without_attr():
    """A config object missing the attr (older pickle / stub) fails open."""
    runner = object.__new__(GatewayRunner)
    runner.config = object()
    assert runner._system_notices_enabled() is True


def test_event_mentions_agent_reads_metadata():
    assert GatewayRunner._event_mentions_agent(_event("group", "a", True)) is True
    assert GatewayRunner._event_mentions_agent(_event("group", "a", False)) is False

    class _NoMeta:
        metadata = None

    assert GatewayRunner._event_mentions_agent(_NoMeta()) is False


# ── behavioral: the busy handler honors both gates end-to-end ─────────────────


class _RecordingAgent:
    """A running agent that records how the gateway tried to disturb it."""

    _supports_active_turn_redirect = True

    def __init__(self):
        self.redirected: list[str] = []
        self.steered: list[str] = []
        self.interrupted: list[Optional[str]] = []

    def redirect(self, text):
        self.redirected.append(text)
        return True

    def steer(self, text):
        self.steered.append(text)
        return True

    def interrupt(self, text=None):
        self.interrupted.append(text)


class _BusyAdapter:
    def __init__(self):
        self.sent: list[str] = []
        self._pending_messages: dict = {}

    async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return True


def _busy_runner(mode: str, *, steer_gate: bool, system_notices: bool):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.SLACK: PlatformConfig(enabled=True, token="***")},
        steer_requires_same_user_mention=steer_gate,
        system_notices_enabled=system_notices,
    )
    runner._draining = False
    runner._restart_requested = False
    runner._session_sources = OrderedDict()
    runner._session_sources_max = 512
    runner._sessions = {}
    runner._pending_messages = {}
    runner._is_user_authorized = lambda _s: True

    adapter = _BusyAdapter()
    runner._adapter_for_source = lambda _s: adapter
    runner._effective_busy_input_mode = lambda _s: mode
    runner._effective_busy_text_mode = lambda _s: "interrupt"
    runner._agent_has_active_subagents = lambda _a: False
    runner._reply_anchor_for_event = lambda _e: None
    runner._thread_metadata_for_source = lambda _s, _a: None
    runner._pending_event_audio_paths = lambda _e: []

    async def _no_compression(_key):
        return False

    async def _steer_text(event):
        return (event.text or "").strip()

    runner._session_has_compression_in_flight = _no_compression
    runner._prepare_busy_steer_text = _steer_text

    for name in (
        "_cache_session_source",
        "_get_cached_session_source",
        "_active_turn_splice_allowed",
        "_system_notices_enabled",
        "_queue_or_replace_pending_event",
        "_queue_depth",
        "_enqueue_fifo",
        "_session_state",
        "_peek_session_state",
        "_sessions_map",
        "_handle_active_session_busy_message",
    ):
        setattr(runner, name, getattr(GatewayRunner, name).__get__(runner, GatewayRunner))

    return runner, adapter


def _make_message_event(chat_type: str, user_id: str, mentioned: bool, text: str):
    from gateway.platforms.base import MessageEvent, MessageType

    src = SessionSource(
        platform=Platform.SLACK,
        chat_id="C-shared",
        chat_type=chat_type,
        user_id=user_id,
        thread_id="t-1",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=src,
        user_id=user_id,
        message_id="m-1",
        metadata={"hermes_is_mentioned": mentioned},
    )


def _install_running_agent(runner) -> tuple[str, _RecordingAgent]:
    session_key = "slack:C-shared:t-1"
    agent = _RecordingAgent()
    state = runner._session_state(session_key)
    state.turn.agent = agent
    return session_key, agent


@pytest.mark.asyncio
async def test_other_user_in_shared_channel_no_redirect_no_ack():
    """Incident path: interrupt-mode message from a DIFFERENT user in a shared
    channel must not redirect the run and must post no ack."""
    runner, adapter = _busy_runner("interrupt", steer_gate=True, system_notices=True)
    session_key, agent = _install_running_agent(runner)
    # Owner of the running turn is alice.
    runner._cache_session_source(
        session_key,
        SessionSource(
            platform=Platform.SLACK, chat_id="C-shared", chat_type="group",
            user_id="alice", thread_id="t-1",
        ),
    )
    event = _make_message_event("group", "bob", mentioned=True, text="do something else")

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert agent.redirected == []       # the run was NOT redirected
    assert agent.interrupted == []      # nor hard-interrupted
    assert adapter.sent == []           # and no "↪ Redirected" ack posted
    # The message is preserved as its own next turn (FIFO slot on the adapter).
    assert session_key in adapter._pending_messages


@pytest.mark.asyncio
async def test_same_user_mention_steer_allowed():
    runner, adapter = _busy_runner("steer", steer_gate=True, system_notices=True)
    session_key, agent = _install_running_agent(runner)
    runner._cache_session_source(
        session_key,
        SessionSource(
            platform=Platform.SLACK, chat_id="C-shared", chat_type="group",
            user_id="alice", thread_id="t-1",
        ),
    )
    event = _make_message_event("group", "alice", mentioned=True, text="also check X")

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert agent.steered == ["also check X"]  # steered into the running turn


@pytest.mark.asyncio
async def test_dm_steer_allowed():
    runner, adapter = _busy_runner("steer", steer_gate=True, system_notices=True)
    session_key, agent = _install_running_agent(runner)
    runner._cache_session_source(
        session_key,
        SessionSource(
            platform=Platform.SLACK, chat_id="C-shared", chat_type="dm",
            user_id="alice", thread_id="t-1",
        ),
    )
    event = _make_message_event("dm", "alice", mentioned=False, text="and also Y")

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert agent.steered == ["and also Y"]


@pytest.mark.asyncio
async def test_gate_off_redirect_unchanged():
    """Flag off ⇒ a different user's interrupt-mode message still redirects
    (stock behavior); ack is posted because system notices are on."""
    runner, adapter = _busy_runner("interrupt", steer_gate=False, system_notices=True)
    session_key, agent = _install_running_agent(runner)
    runner._cache_session_source(
        session_key,
        SessionSource(
            platform=Platform.SLACK, chat_id="C-shared", chat_type="group",
            user_id="alice", thread_id="t-1",
        ),
    )
    event = _make_message_event("group", "bob", mentioned=False, text="new direction")

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert agent.redirected == ["new direction"]  # unchanged upstream behavior
    assert any("Redirected current run" in m for m in adapter.sent)


@pytest.mark.asyncio
async def test_system_notices_off_suppresses_busy_ack_but_still_redirects():
    """system_notices off ⇒ the run is still redirected (the work happens) but
    no "↪ Redirected" ack reaches the channel. Steer gate off so redirect runs."""
    runner, adapter = _busy_runner("interrupt", steer_gate=False, system_notices=False)
    session_key, agent = _install_running_agent(runner)
    runner._cache_session_source(
        session_key,
        SessionSource(
            platform=Platform.SLACK, chat_id="C-shared", chat_type="dm",
            user_id="alice", thread_id="t-1",
        ),
    )
    event = _make_message_event("dm", "alice", mentioned=False, text="new direction")

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert agent.redirected == ["new direction"]  # input still processed
    assert adapter.sent == []                       # but ack suppressed


# ── source tripwire: the new per-turn banner phrases stay centralized ─────────


def _runtime_py_files():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    skip = ("/tests/", "/__pycache__/", "/.git/", "/node_modules/")
    return [
        p
        for p in root.rglob("*.py")
        if not any(part in str(p) for part in skip)
    ]


def _files_containing(phrase: str) -> set[str]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    hits: set[str] = set()
    for p in _runtime_py_files():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if phrase in line:
                hits.add(str(p.relative_to(root)))
                break
    return hits


# Each emitted per-turn notice string and the ONLY files allowed to carry it
# (on a non-comment line). ``tui_gateway/server.py`` carries a docstring that
# names the self-improvement summary to document its config knob — a documented,
# non-emitting exception (mirrors the P9 tripwire's discord-adapter exception).
_REDIRECT = "↪ Redirected current run"
_SELF_IMPROVE = "💾 Self-improvement review:"
_FIRST_TIP = "💡 First-time tip"
_ALLOWED_NOTICE_FILES = {
    _REDIRECT: {"gateway/run.py"},
    _SELF_IMPROVE: {"agent/background_review.py", "tui_gateway/server.py"},
    _FIRST_TIP: {"agent/onboarding.py"},
}


@pytest.mark.parametrize("phrase,allowed", _ALLOWED_NOTICE_FILES.items())
def test_new_notice_phrases_are_centralized(phrase, allowed):
    """A per-turn notice string must not sprout a new, ungated emission site."""
    stray = _files_containing(phrase) - allowed
    assert not stray, (
        f"System-notice phrase {phrase!r} appeared in unexpected file(s): "
        f"{sorted(stray)}. These per-turn notices must live only in "
        f"{sorted(allowed)} and stay gated behind "
        f"gateway.system_notices_enabled (fork patch P13)."
    )


def test_notice_phrases_still_present_in_canonical_module():
    """Guard the tripwire: if a notice string is refactored away, fail loudly
    rather than passing vacuously."""
    assert "gateway/run.py" in _files_containing(_REDIRECT)
    assert "agent/background_review.py" in _files_containing(_SELF_IMPROVE)
    assert "agent/onboarding.py" in _files_containing(_FIRST_TIP)
