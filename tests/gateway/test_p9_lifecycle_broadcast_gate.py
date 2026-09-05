"""Fork patch P9 — the lifecycle-broadcast master gate.

``GatewayConfig.lifecycle_broadcasts_enabled`` (default True = stock upstream
behavior) turns OFF every unprompted, system-generated runtime lifecycle
notice the gateway can push to a platform. HQ fleet boxes render it false so a
fleet agent never posts robotic "⚠️ Gateway shutting down — Your current task
will be interrupted." boilerplate into a customer Slack channel (incident
2026-09-04; policy
``indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages``).

Two kinds of coverage:
  * behavioral — with the flag off, the shutdown / startup / restart notifiers
    produce ZERO outbound platform messages, while the default (flag on) still
    broadcasts (so the gate cannot silently become always-off);
  * a source tripwire — the user-facing banner phrases live only in the single
    ``agent/hq_branding.py`` module, so a later change cannot re-introduce a
    free-floating robotic lifecycle banner elsewhere without failing a test.
"""

import sys
from pathlib import Path

import pytest

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


# ── behavioral: flag OFF ⇒ zero outbound platform messages ───────────────────


@pytest.mark.asyncio
async def test_shutdown_produces_zero_platform_messages_when_lifecycle_disabled():
    """The exact incident path: a restart-driven shutdown with an active
    session and a home channel must emit nothing when the master gate is off."""
    runner, adapter = make_restart_runner()
    runner.config.lifecycle_broadcasts_enabled = False
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )

    source = make_restart_source(chat_id="active-42", chat_type="group", thread_id="t-7")
    session_key = build_session_key(source)
    runner._running_agents[session_key] = object()
    runner._cache_session_source(session_key, source)
    runner._restart_requested = True

    await runner._notify_active_sessions_of_shutdown()
    delivered = await runner._send_home_channel_startup_notifications()

    assert adapter.sent == [], f"expected zero sends, got {adapter.sent!r}"
    assert delivered == set()


@pytest.mark.asyncio
async def test_home_channel_startup_silent_when_lifecycle_disabled():
    runner, adapter = make_restart_runner()
    runner.config.lifecycle_broadcasts_enabled = False
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home-42", name="Ops Home"
    )

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == set()
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_restart_notification_suppressed_and_marker_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".restart_notify.json"
    marker.write_text('{"platform": "telegram", "chat_id": "42"}', encoding="utf-8")

    runner, adapter = make_restart_runner()
    runner.config.lifecycle_broadcasts_enabled = False

    result = await runner._send_restart_notification()

    assert result is None
    assert adapter.sent == []
    # Marker cleared so the banner cannot leak on a later flag flip.
    assert not marker.exists()


# ── behavioral: flag ON (default) ⇒ stock behavior still broadcasts ──────────


@pytest.mark.asyncio
async def test_shutdown_still_broadcasts_when_lifecycle_enabled():
    """Default (flag on) reproduces stock upstream behavior — the gate must
    not silently suppress everything."""
    runner, adapter = make_restart_runner()
    assert runner.config.lifecycle_broadcasts_enabled is True  # dataclass default

    source = make_restart_source(chat_id="active-42", chat_type="group", thread_id="t-7")
    session_key = build_session_key(source)
    runner._running_agents[session_key] = object()
    runner._cache_session_source(session_key, source)

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent == [
        "⚠️ Gateway shutting down — Your current task will be interrupted."
    ]


# ── source tripwire: robotic banner phrases stay in one module ───────────────


def _runtime_py_files() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    skip = ("/tests/", "/__pycache__/", "/.git/", "/node_modules/")
    files: list[Path] = []
    for p in root.rglob("*.py"):
        s = str(p)
        if any(part in s for part in skip):
            continue
        files.append(p)
    return files


def _files_containing(phrase: str) -> set[str]:
    """Relative paths of runtime files with ``phrase`` on a non-comment line."""
    root = Path(__file__).resolve().parents[2]
    hits: set[str] = set()
    for p in _runtime_py_files():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue  # full-line comment — not a shippable string
            if phrase in line:
                hits.add(str(p.relative_to(root)))
                break
    return hits


# Each user-facing lifecycle banner phrase and the ONLY files allowed to carry
# it. ``agent/hq_branding.py`` is the single source of truth for the strings;
# the discord adapter carries a suppression regex that references the comeback
# banners to filter echoes, and is an explicit, documented exception.
_BRANDING = "agent/hq_branding.py"
_DISCORD = "plugins/platforms/discord/adapter.py"
_ALLOWED = {
    # The incident phrase — nothing but the branding module may contain it.
    "Your current task will be interrupted": {_BRANDING},
    "restarted successfully. Your session continues": {_BRANDING, _DISCORD},
    "is back and ready": {_BRANDING, _DISCORD},
}


@pytest.mark.parametrize("phrase,allowed", _ALLOWED.items())
def test_lifecycle_banner_phrases_are_centralized(phrase, allowed):
    hits = _files_containing(phrase)
    stray = hits - allowed
    assert not stray, (
        f"Robotic lifecycle banner phrase {phrase!r} appeared in "
        f"unexpected file(s): {sorted(stray)}. User-facing lifecycle strings "
        f"must live only in {sorted(allowed)} and be gated behind "
        f"gateway.lifecycle_broadcasts_enabled (fork patch P9)."
    )


def test_incident_phrase_exists_in_branding_module():
    """Guard the tripwire itself: if branding is refactored so the phrase moves
    or vanishes, this fails loudly rather than passing vacuously."""
    assert _files_containing("Your current task will be interrupted") == {_BRANDING}


# ── P9 extension: progress / "still working" heartbeat is lifecycle chatter ──
#
# The "⏳ Working — N min" long-running heartbeat is unprompted, system-generated
# runtime chatter of the same class as the shutdown/startup banners. HQ boxes set
# ``lifecycle_broadcasts_enabled=False``, so the heartbeat gate
# (``_should_emit_long_running_notification``) must refuse to emit — no progress
# notice reaches a chat platform (live-smoke 2026-09-05, webclient DM leak of
# "⏳ Working, 3 min, iteration 13/9223372036854775807, receiving stream response").


def test_progress_heartbeat_suppressed_for_hq_boxes():
    """With the master lifecycle gate off, the long-running progress heartbeat
    must not emit even while the run legitimately owns its session slot."""
    runner, _adapter = make_restart_runner()
    runner.config.lifecycle_broadcasts_enabled = False
    runner._peek_session_state = lambda _key: None

    agent = object()
    should_emit = gateway_run.GatewayRunner._should_emit_long_running_notification(
        runner, session_key=None, agent=agent, executor_task=None
    )
    assert should_emit is False


def test_progress_heartbeat_emits_by_default():
    """Default (stock upstream) behavior keeps the heartbeat so the gate cannot
    silently become always-off."""
    runner, _adapter = make_restart_runner()
    runner.config.lifecycle_broadcasts_enabled = True
    runner._peek_session_state = lambda _key: None

    agent = object()
    should_emit = gateway_run.GatewayRunner._should_emit_long_running_notification(
        runner, session_key=None, agent=agent, executor_task=None
    )
    assert should_emit is True


# ── iteration denominator: omit when max_iterations is unset (INT64_MAX) ──────


def test_iteration_denominator_omitted_when_max_unset():
    """``AIAgent.max_iterations`` defaults to ``sys.maxsize`` (no real cap).
    The denominator must be omitted rather than leaking 9223372036854775807."""
    assert gateway_run._format_iteration_progress(13, sys.maxsize) == "iteration 13"
    # None and non-positive sentinels are also "no cap".
    assert gateway_run._format_iteration_progress(13, None) == "iteration 13"
    assert gateway_run._format_iteration_progress(13, 0) == "iteration 13"
    assert gateway_run._format_iteration_progress(13, -1) == "iteration 13"


def test_iteration_denominator_shown_when_real_cap_set():
    """A genuine cap still renders the denominator."""
    assert gateway_run._format_iteration_progress(3, 40) == "iteration 3/40"


def test_iteration_progress_survives_bad_inputs():
    assert gateway_run._format_iteration_progress(None, 40) == ""
    assert gateway_run._format_iteration_progress("x", 40) == ""
    assert gateway_run._format_iteration_progress(5, "nan") == "iteration 5"
