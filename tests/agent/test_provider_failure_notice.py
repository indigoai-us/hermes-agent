"""Tripwire + behavior tests for the P17 in-voice provider-failure composer.

Covers:
  * classification of billing / auth / rate-limit / outage / generic,
  * in-voice composition (first person, owner action, provider naming),
  * tripwires: no robotic phrasing, no emoji, no raw JSON, no HTTP status
    lines, no vendor URLs in ANY composed string,
  * the per-conversation cooldown gate (once per class per window; never two
    back to back).
"""

import itertools

import pytest

from agent import provider_failure_notice as pfn


# ── Raw failure bodies observed in the wild (the incident inputs) ────────────

GROK_BILLING_403 = (
    'Billing or credits exhausted: HTTP 403: {"code":'
    '"personal-team-blocked:spending-limit","error":"You have run out of '
    'credits or need a Grok subscription. Add credits at '
    'https://grok.com/?_s=usage"}'
)
GATEWAY_RATE_LIMIT = "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again."
GATEWAY_GENERIC = (
    "⚠️ The model provider failed after retries. I kept raw provider details "
    "out of chat; check gateway logs for diagnostics."
)
CODEX_NOT_LOGGED_IN = "provider authentication failed: not logged in (HTTP 401)"
OUTAGE = "httpx.ConnectError: All connection attempts failed"

# Forbidden fragments that must NEVER appear in a user-facing composed notice.
FORBIDDEN_FRAGMENTS = [
    "⚠",
    "⏱",
    "❌",
    "HTTP 403",
    "HTTP 401",
    "http 403",
    '{"code"',
    "{'code'",
    "grok.com",
    "x.ai",
    "spending-limit",
    "personal-team-blocked",
    "rate-limiting requests",
    "check gateway logs",
    "raw provider details",
    "https://",
    "http://",
]


def _all_composed_notices():
    """Every notice the composer can produce across classes/providers/owners."""
    classes = [
        pfn.CLASS_BILLING,
        pfn.CLASS_AUTH,
        pfn.CLASS_RATE_LIMIT,
        pfn.CLASS_OUTAGE,
        pfn.CLASS_GENERIC,
    ]
    providers = [None, "grok", "xai", "openai", "codex", "anthropic", "mystery"]
    owners = [None, "Corey", "the owner"]
    for cls, prov, owner in itertools.product(classes, providers, owners):
        yield cls, prov, owner, pfn.compose_provider_failure_notice(
            cls, provider=prov, owner_name=owner
        )


# ── Classification ───────────────────────────────────────────────────────────

def test_classifies_grok_spending_limit_as_billing():
    assert pfn.classify_failure_text(GROK_BILLING_403) == pfn.CLASS_BILLING
    assert pfn.detect_provider(GROK_BILLING_403) == "grok"


def test_classifies_rate_limit():
    assert pfn.classify_failure_text(GATEWAY_RATE_LIMIT) == pfn.CLASS_RATE_LIMIT


def test_classifies_auth():
    assert pfn.classify_failure_text(CODEX_NOT_LOGGED_IN) == pfn.CLASS_AUTH


def test_classifies_outage():
    assert pfn.classify_failure_text(OUTAGE) == pfn.CLASS_OUTAGE


def test_generic_envelope():
    assert pfn.classify_failure_text("API call failed") == pfn.CLASS_GENERIC


def test_billing_wins_over_generic_403_shape():
    # A Grok spending-limit is a 403 (auth-shaped) AND an HTTP envelope, but the
    # billing verdict must win so the owner is told to top up, not reconnect.
    assert pfn.classify_failure_text(GROK_BILLING_403) == pfn.CLASS_BILLING


def test_ordinary_prose_is_not_a_failure():
    prose = (
        "Sure — a 429 status code means the server received too many requests. "
        "Here's how rate limiting usually works in an API: the server tracks "
        "your request count in a rolling window and returns HTTP 429 once you "
        "exceed the quota. You can handle it by backing off and retrying. "
        "Would you like a code example in Python or JavaScript?"
    )
    assert pfn.classify_failure_text(prose) is None


def test_empty_and_none_are_not_failures():
    assert pfn.classify_failure_text("") is None
    assert pfn.classify_failure_text("   ") is None


# ── Tripwires: forbidden fragments in every composed notice ──────────────────

@pytest.mark.parametrize("fragment", FORBIDDEN_FRAGMENTS)
def test_no_forbidden_fragment_in_any_composed_notice(fragment):
    for cls, prov, owner, notice in _all_composed_notices():
        assert fragment.lower() not in notice.lower(), (
            f"forbidden {fragment!r} leaked in class={cls} provider={prov} "
            f"owner={owner}: {notice!r}"
        )


def test_composed_notices_have_no_emoji():
    for _cls, _prov, _owner, notice in _all_composed_notices():
        assert all(ord(ch) < 0x2190 for ch in notice), f"emoji/symbol in {notice!r}"


def test_composed_notices_are_first_person():
    for _cls, _prov, _owner, notice in _all_composed_notices():
        assert notice.lower().startswith("i") or notice.lower().startswith("my"), notice


# ── In-voice content ─────────────────────────────────────────────────────────

def test_billing_names_owner_action_and_provider():
    n = pfn.compose_provider_failure_notice(
        pfn.CLASS_BILLING, provider="grok", owner_name="Corey"
    )
    assert "Grok" in n
    assert "Corey" in n
    assert "credits are out" in n
    assert "top them up" in n


def test_billing_defaults_owner_to_the_owner():
    n = pfn.compose_provider_failure_notice(pfn.CLASS_BILLING, provider="grok")
    assert "the owner" in n


def test_auth_asks_owner_to_reconnect():
    n = pfn.compose_provider_failure_notice(pfn.CLASS_AUTH, provider="codex")
    assert "reconnect" in n
    assert "Codex" in n


def test_rate_limit_does_not_summon_owner():
    n = pfn.compose_provider_failure_notice(pfn.CLASS_RATE_LIMIT, provider="grok")
    assert "the owner" not in n.lower()
    assert "top" not in n.lower()


def test_unknown_provider_says_my_model():
    n = pfn.compose_provider_failure_notice(pfn.CLASS_BILLING, provider="mystery")
    assert "my model" in n.lower()


def test_compose_from_text_end_to_end():
    n = pfn.compose_from_text(GROK_BILLING_403, owner_name="Corey")
    assert n is not None
    assert "Grok" in n and "Corey" in n
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment.lower() not in n.lower()


def test_compose_from_text_returns_none_for_prose():
    assert pfn.compose_from_text("Here is your report, all done!") is None


# ── Structured log line ──────────────────────────────────────────────────────

def test_structured_log_line_tokens():
    line = pfn.structured_log_line(pfn.CLASS_BILLING, "grok", conversation="hqdm:c1:t1")
    assert line.startswith("provider_failure ")
    assert "class=billing" in line
    assert "provider=grok" in line
    assert "status=out-of-credits" in line
    assert "conversation=hqdm:c1:t1" in line


def test_structured_log_status_mapping():
    assert "status=runtime-auth-failed" in pfn.structured_log_line(pfn.CLASS_AUTH, "codex")
    assert "status=rate-limited" in pfn.structured_log_line(pfn.CLASS_RATE_LIMIT, "grok")


# ── Cooldown gate ────────────────────────────────────────────────────────────

class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_cooldown_emits_once_then_suppresses():
    clock = _FakeClock()
    gate = pfn.ProviderFailureNoticeGate(cooldown_seconds=3600, clock=clock)
    key = "hqdm:chatA:threadA"
    assert gate.should_emit(key, pfn.CLASS_BILLING) is True   # first: emit
    assert gate.should_emit(key, pfn.CLASS_BILLING) is False  # same turn: suppress
    clock.advance(60)
    assert gate.should_emit(key, pfn.CLASS_BILLING) is False  # 1 min later: suppress
    clock.advance(3600)
    assert gate.should_emit(key, pfn.CLASS_BILLING) is True   # after window: emit


def test_cooldown_is_per_conversation():
    clock = _FakeClock()
    gate = pfn.ProviderFailureNoticeGate(cooldown_seconds=3600, clock=clock)
    assert gate.should_emit("conv1", pfn.CLASS_BILLING) is True
    # A different conversation is independent — first failure there still speaks.
    assert gate.should_emit("conv2", pfn.CLASS_BILLING) is True
    assert gate.should_emit("conv1", pfn.CLASS_BILLING) is False


def test_cooldown_is_per_class():
    clock = _FakeClock()
    gate = pfn.ProviderFailureNoticeGate(cooldown_seconds=3600, clock=clock)
    key = "conv1"
    assert gate.should_emit(key, pfn.CLASS_BILLING) is True
    # A different failure class in the same conversation may still speak once.
    assert gate.should_emit(key, pfn.CLASS_AUTH) is True
    assert gate.should_emit(key, pfn.CLASS_BILLING) is False


def test_no_conversation_key_disables_rate_limit():
    gate = pfn.ProviderFailureNoticeGate(cooldown_seconds=3600)
    assert gate.should_emit(None, pfn.CLASS_BILLING) is True
    assert gate.should_emit(None, pfn.CLASS_BILLING) is True
    assert gate.should_emit("", pfn.CLASS_BILLING) is True


def test_zero_cooldown_disables_rate_limit():
    gate = pfn.ProviderFailureNoticeGate(cooldown_seconds=0)
    assert gate.should_emit("conv1", pfn.CLASS_BILLING) is True
    assert gate.should_emit("conv1", pfn.CLASS_BILLING) is True


def test_reset_clears_state():
    clock = _FakeClock()
    gate = pfn.ProviderFailureNoticeGate(cooldown_seconds=3600, clock=clock)
    assert gate.should_emit("conv1", pfn.CLASS_BILLING) is True
    assert gate.should_emit("conv1", pfn.CLASS_BILLING) is False
    gate.reset("conv1")
    assert gate.should_emit("conv1", pfn.CLASS_BILLING) is True
