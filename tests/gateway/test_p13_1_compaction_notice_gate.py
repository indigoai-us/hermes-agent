"""Fork patch P13.1 — the Codex gpt-5.x compaction autoraise notice never
reaches a chat surface on an HQ box.

The notice is built in ``agent/agent_init.py`` and replayed to every chat
platform via ``agent._compression_warning`` as a ``lifecycle`` status. It flows
through the module-level ``_prepare_gateway_status_message`` chokepoint, which
P13 did not gate (P13's ``system_notices_enabled`` covered only the
instance-method notices). On 2026-09 it posted into the customer channel
``lilo-social`` (agent Stitch). This locks in the suppression and the exact
offending phrases so a later change cannot re-introduce the banner.

Policy: indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages.
"""

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.run import _prepare_gateway_status_message

# The exact banner the runtime builds (agent/agent_init.py
# _build_codex_gpt5_autoraise_notice). Reproduced verbatim so the tripwire
# fails if either the emitter wording or the suppressor regex drifts apart.
AUTORAISE_NOTICE = (
    "ℹ Codex gpt-5.6-sol caps context at 272K, so auto-compaction was raised "
    "to 85% (from 50%) to use more of the window before summarizing.\n"
    "  Opt back out: hermes config set compression.codex_gpt55_autoraise false"
)

# Individual tripwire phrases the task enumerates. Each must be suppressed on a
# chat surface when the master gate is off.
TRIPWIRE_PHRASES = [
    "ℹ auto-compaction was raised to 85%",
    "auto-compaction was raised to 85% (from 50%)",
    "Opt back out: hermes config set compression.codex_gpt55_autoraise false",
    "ℹ️ some banner text",  # Slack renders the info glyph with the VS16 emoji selector
]

CHAT_PLATFORMS = [Platform.SLACK, Platform.TELEGRAM]


@pytest.fixture
def notices_disabled(monkeypatch):
    """HQ box: system_notices_enabled=false (top-level key)."""
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda *a, **k: {"system_notices_enabled": False},
    )


@pytest.fixture
def notices_disabled_nested(monkeypatch):
    """HQ box: system_notices_enabled=false under the nested gateway.* block."""
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda *a, **k: {"gateway": {"system_notices_enabled": False}},
    )


@pytest.fixture
def notices_enabled(monkeypatch):
    """Stock box: no gate set ⇒ default True ⇒ notice passes through."""
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda *a, **k: {})


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_autoraise_notice_suppressed_on_hq_box(platform, notices_disabled):
    assert (
        _prepare_gateway_status_message(platform, "lifecycle", AUTORAISE_NOTICE)
        is None
    )


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_autoraise_notice_suppressed_nested_gate(platform, notices_disabled_nested):
    assert (
        _prepare_gateway_status_message(platform, "lifecycle", AUTORAISE_NOTICE)
        is None
    )


@pytest.mark.parametrize("phrase", TRIPWIRE_PHRASES)
def test_each_tripwire_phrase_suppressed(phrase, notices_disabled):
    assert _prepare_gateway_status_message(Platform.SLACK, "lifecycle", phrase) is None


def test_notice_passes_through_when_gate_default_on(notices_enabled):
    # Default (stock upstream) keeps the notice — the patch must not change
    # behavior on non-HQ boxes.
    out = _prepare_gateway_status_message(Platform.SLACK, "lifecycle", AUTORAISE_NOTICE)
    assert out is not None
    assert "auto-compaction was raised" in out


def test_ordinary_reply_untouched_even_when_gate_off(notices_disabled):
    # A normal assistant status with none of the notice phrases must still flow.
    msg = "Pulling the last two days of Meta campaign data now."
    assert (
        _prepare_gateway_status_message(Platform.SLACK, "lifecycle", msg) == msg
    )


def test_source_tripwire_emitter_and_suppressor_agree():
    # The suppressor regex must match every phrase the emitter can produce.
    assert gateway_run._SYSTEM_NOTICE_STATUS_RE.search(AUTORAISE_NOTICE)
    assert gateway_run._SYSTEM_NOTICE_STATUS_RE.search("hermes config set x y")
    assert gateway_run._SYSTEM_NOTICE_STATUS_RE.search("Opt back out: foo")
    assert gateway_run._SYSTEM_NOTICE_STATUS_RE.search("ℹ️ x")
