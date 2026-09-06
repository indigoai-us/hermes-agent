"""Gateway wiring for the P17 in-voice provider-failure composer.

Verifies that, when ``gateway.provider_failure_voice_enabled`` is on, both
gateway chokepoints (`_sanitize_gateway_final_response` for the final reply,
`_prepare_gateway_status_message` for status lines) route provider failures
through the single in-voice composer, apply the per-conversation cooldown, emit
the structured telemetry line, and never leak raw JSON / HTTP status text /
vendor URLs. Default-off keeps stock behavior.
"""

import logging

import pytest

import gateway.run as gateway_run
from gateway.run import (
    _prepare_gateway_status_message,
    _sanitize_gateway_final_response,
)

CHAT = "telegram"

# The exact leaky final_response the incident produced (long, multi-line — it
# bypassed the pre-P17 short/anchored gateway detector and shipped verbatim).
GROK_BILLING_FINAL = (
    'Billing or credits exhausted: HTTP 403: {"code":'
    '"personal-team-blocked:spending-limit","error":"You have run out of '
    'credits or need a Grok subscription. Add credits at '
    'https://grok.com/?_s=usage"}\n'
    "You can switch providers temporarily with /model <model> --provider <provider>.\n"
    "xAI billing: https://console.x.ai/team/default/billing"
)

GATEWAY_RATE_LIMIT_LEGACY = (
    "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again."
)

# Fragments that must never reach a chat surface.
FORBIDDEN = [
    "⚠", "⏱", "❌", "HTTP 403", '{"code"', "grok.com", "x.ai",
    "spending-limit", "rate-limiting requests", "check gateway logs",
    "https://", "/model",
]


@pytest.fixture(autouse=True)
def _reset_gate():
    gateway_run._GATEWAY_PROVIDER_FAILURE_GATE.reset()
    yield
    gateway_run._GATEWAY_PROVIDER_FAILURE_GATE.reset()


@pytest.fixture
def voice_enabled(monkeypatch):
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda *a, **k: {
            "gateway": {
                "provider_failure_voice_enabled": True,
                "provider_failure_notice_cooldown_seconds": 3600,
            }
        },
    )


@pytest.fixture
def voice_disabled(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda *a, **k: {})


def _assert_clean(text: str):
    low = text.lower()
    for frag in FORBIDDEN:
        assert frag.lower() not in low, f"forbidden {frag!r} leaked: {text!r}"


# ── Final-response path ──────────────────────────────────────────────────────

def test_final_billing_is_recomposed_in_voice(voice_enabled):
    out = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    assert "credits are out" in out
    assert "Grok" in out
    _assert_clean(out)


def test_final_rate_limit_legacy_is_recomposed(voice_enabled):
    out = _sanitize_gateway_final_response(CHAT, GATEWAY_RATE_LIMIT_LEGACY, "hqdm:cA:tA")
    assert "throttled" in out.lower()
    _assert_clean(out)


def test_final_ordinary_answer_untouched(voice_enabled):
    answer = "Here's the summary you asked for: revenue grew 12% last quarter."
    out = _sanitize_gateway_final_response(CHAT, answer, "hqdm:cA:tA")
    assert out == answer


def test_final_disabled_is_stock_not_composed(voice_disabled):
    out = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    # Stock path does not produce the in-voice line.
    assert "credits are out" not in out


# ── Cooldown: never two back to back, silence on repeat ──────────────────────

def test_cooldown_suppresses_second_notice_same_conversation(voice_enabled):
    first = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    assert "credits are out" in first
    # A second failure in the same conversation within the window is silenced
    # (empty final ⇒ nothing sent) — never a second robotic line.
    second = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    assert second == ""


def test_final_then_status_do_not_double_post(voice_enabled):
    # One turn can hit both chokepoints; the shared gate collapses them to one.
    final = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    assert "credits are out" in final
    status = _prepare_gateway_status_message(
        CHAT, "lifecycle", "❌ Billing or credits exhausted — HTTP 403", "hqdm:cA:tA"
    )
    assert status is None  # suppressed: already announced this class


def test_cooldown_is_per_conversation(voice_enabled):
    a = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    b = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cB:tB")
    assert "credits are out" in a
    assert "credits are out" in b  # a different conversation still gets told once


# ── Status path ──────────────────────────────────────────────────────────────

def test_status_billing_is_recomposed(voice_enabled):
    out = _prepare_gateway_status_message(
        CHAT, "lifecycle", "❌ Billing or credits exhausted — HTTP 403", "hqdm:cA:tA"
    )
    assert out is not None
    assert "credits are out" in out
    _assert_clean(out)


# ── Structured telemetry ─────────────────────────────────────────────────────

def test_structured_log_line_emitted(voice_enabled, caplog):
    with caplog.at_level(logging.WARNING, logger=gateway_run.logger.name):
        _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "provider_failure" in joined
    assert "class=billing" in joined
    assert "provider=grok" in joined
    assert "status=out-of-credits" in joined


def test_structured_log_emitted_even_when_suppressed(voice_enabled, caplog):
    _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    with caplog.at_level(logging.WARNING, logger=gateway_run.logger.name):
        # second (suppressed) call must still log telemetry
        out = _sanitize_gateway_final_response(CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA")
    assert out == ""
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "class=billing" in joined


# ── P17.1: never rewrite a SUCCESSFUL turn ───────────────────────────────────

def test_successful_turn_is_never_recomposed_even_if_error_shaped(voice_enabled):
    # A successful reply (finish_reason=stop) that merely looks error-shaped —
    # e.g. begins with a status code — must be returned VERBATIM when the caller
    # signals the turn did not fail. This is the core P17.1 guard.
    answer = "835 files."
    out = _sanitize_gateway_final_response(
        CHAT, answer, "hqdm:cA:tA", False  # turn_had_provider_error=False
    )
    assert out == answer


def test_short_numeric_answer_survives_even_when_gate_defaults_on(voice_enabled):
    # Belt-and-suspenders: even if a caller cannot signal outcome (defaults to
    # True), the classifier fix alone keeps a short numeric answer untouched.
    answer = "829 files."
    out = _sanitize_gateway_final_response(CHAT, answer, "hqdm:cA:tA")
    assert out == answer


def test_provider_error_turn_is_still_recomposed_when_flagged(voice_enabled):
    # The incident case: a real billing failure (turn_had_provider_error=True)
    # is still recomposed into the in-voice line.
    out = _sanitize_gateway_final_response(
        CHAT, GROK_BILLING_FINAL, "hqdm:cA:tA", True
    )
    assert "credits are out" in out
    _assert_clean(out)


def test_result_signals_provider_error_helper():
    from gateway.run import _result_signals_provider_error as sig
    assert sig({"failed": True}) is True
    assert sig({"error": "boom"}) is True
    assert sig({"failure_reason": "billing"}) is True
    assert sig({"billing_block": {"provider": "grok"}}) is True
    # A clean successful turn — nothing set.
    assert sig({"final_response": "835 files.", "failed": False,
                "error": None, "failure_reason": None}) is False
    assert sig({}) is False
    # Odd shapes fail safe to True (never silently pass a real failure).
    assert sig(None) is True


# ── Raw-text surfaces keep diagnostics ───────────────────────────────────────

def test_local_surface_keeps_raw(voice_enabled):
    out = _sanitize_gateway_final_response("local", GROK_BILLING_FINAL, "local")
    assert out == GROK_BILLING_FINAL  # CLI/TUI diagnostics unchanged
