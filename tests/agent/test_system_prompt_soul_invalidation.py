"""Fork patch P18 (hq/v2): SOUL/persona-change system-prompt invalidation.

Stock hermes stamps + re-checks the capability epoch ONLY on Bot Chat prompts,
so on HQ boxes a SOUL.md edit never invalidated a continuing session's cached
system prompt — a bot kept a stale persona (wrong name, wrong voice) across
releases until a restart / /new / compression. When
``agent.system_prompt_invalidate_on_soul_change`` is on, EVERY built prompt
carries the capability-epoch stamp and the restore path rebuilds once when the
SOUL/capability surface drifts.

These tests pin:
  * the builder stamps the epoch on an ordinary (non-Bot-Chat) session only when
    the flag is on (off ⇒ upstream behavior, no stamp),
  * the restore path rebuilds when SOUL changes and reuses byte-for-byte when it
    does not (prefix-cache stability preserved),
  * a stale ordinary session is NOT mis-titled "Bot Chat" on rebuild.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from tools.bot_mode_probe import capability_fingerprint, epoch_line
from hermes_constants import (
    reset_hermes_home_override,
    set_hermes_home_override,
)


# ── builder-side stamping ────────────────────────────────────────────────────


def _real_agent(session_id: str = "p18-test"):
    from run_agent import AIAgent

    return AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        provider="openrouter",
        platform="cli",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id=session_id,
    )


def _pin_workspace(monkeypatch):
    # build_coding_workspace_block shells out to live git; pin it so unrelated
    # git contention can't flake these prompt-content assertions.
    monkeypatch.setattr(
        "agent.coding_context.build_coding_workspace_block",
        lambda cwd=None: "Workspace (snapshot at session start):\n- Root: /pinned",
    )


def test_builder_omits_epoch_stamp_for_ordinary_session_when_flag_off(monkeypatch):
    _pin_workspace(monkeypatch)
    from agent.system_prompt import build_system_prompt

    agent = _real_agent()
    # Default off, ordinary (non-"Bot Chat") session.
    agent._system_prompt_invalidate_on_soul_change = False
    agent._session_title_hint = None

    prompt = build_system_prompt(agent)
    assert "Capability epoch:" not in prompt


def test_builder_stamps_epoch_for_ordinary_session_when_flag_on(monkeypatch):
    _pin_workspace(monkeypatch)
    from agent.system_prompt import build_system_prompt

    agent = _real_agent()
    agent._system_prompt_invalidate_on_soul_change = True
    agent._session_title_hint = None  # NOT a Bot Chat

    prompt = build_system_prompt(agent)
    # The ordinary session now carries the stamp — the whole point of P18.
    assert "Capability epoch:" in prompt


# ── restore-side invalidation ────────────────────────────────────────────────


def _make_agent(session_db, prebuilt_prompt="REBUILT_PROMPT_V2"):
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "p18-restore"
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent.platform = "cli"
    agent._session_db = session_db
    agent._use_prompt_caching = False
    agent._bot_mode_protocol = True
    agent._session_title_hint = None
    agent._build_system_prompt = MagicMock(return_value=prebuilt_prompt)
    return agent


@pytest.fixture
def home(tmp_path):
    (tmp_path / "SOUL.md").write_text("Persona v1", encoding="utf-8")
    token = set_hermes_home_override(str(tmp_path))
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def _stamped_stored_prompt(home_path):
    # A minimal prompt whose runtime-identity lines are absent (so the runtime
    # match passes) but which carries the capability-epoch stamp for the home.
    return "You are Hermes Agent.\n" + epoch_line(home_path)


def test_unchanged_soul_reuses_prompt_verbatim(home):
    stored = _stamped_stored_prompt(home)
    db = MagicMock()
    db.get_session.return_value = {"system_prompt": stored}
    db.get_session_title.return_value = None
    agent = _make_agent(db)

    _restore_or_build_system_prompt(
        agent, None, [{"role": "user", "content": "hi"}]
    )

    # SOUL unchanged ⇒ epoch matches ⇒ stored bytes reused, no rebuild.
    assert agent._cached_system_prompt == stored
    agent._build_system_prompt.assert_not_called()
    db.update_system_prompt.assert_not_called()


def test_changed_soul_rebuilds_prompt_next_turn(home, caplog):
    stored = _stamped_stored_prompt(home)  # embeds the v1 epoch
    db = MagicMock()
    db.get_session.return_value = {"system_prompt": stored}
    db.get_session_title.return_value = None
    agent = _make_agent(db)

    # The user edits the bot's SOUL between turns.
    (home / "SOUL.md").write_text("Persona v2 — new name and voice", encoding="utf-8")
    # Sanity: the fingerprint really moved off the one embedded in ``stored``.
    assert capability_fingerprint(home) not in stored

    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _restore_or_build_system_prompt(
            agent, None, [{"role": "user", "content": "hi"}]
        )

    # Stale epoch ⇒ exactly one rebuild, persisted for verbatim reuse next turn.
    assert agent._cached_system_prompt == "REBUILT_PROMPT_V2"
    agent._build_system_prompt.assert_called_once()
    db.update_system_prompt.assert_called_once_with(
        agent.session_id, "REBUILT_PROMPT_V2"
    )
    # The ordinary session must NOT be re-titled "Bot Chat" on rebuild.
    assert agent._session_title_hint != "Bot Chat"
    assert any(
        "epoch changed" in r.getMessage() for r in caplog.records
    )


def test_unstamped_prompt_is_reused_even_if_soul_changes(home):
    # Flag-off sessions never carry the stamp; they must be reused byte-for-byte
    # (upstream behavior) regardless of SOUL edits — no accidental rebuilds.
    stored = "You are Hermes Agent.\n(no epoch stamp here)"
    db = MagicMock()
    db.get_session.return_value = {"system_prompt": stored}
    db.get_session_title.return_value = None
    agent = _make_agent(db)

    (home / "SOUL.md").write_text("totally different persona", encoding="utf-8")

    _restore_or_build_system_prompt(
        agent, None, [{"role": "user", "content": "hi"}]
    )

    assert agent._cached_system_prompt == stored
    agent._build_system_prompt.assert_not_called()
