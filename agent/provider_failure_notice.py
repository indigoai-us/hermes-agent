"""In-voice provider-failure notices (fork patch P17, hq/v2).

One composer for every user-facing provider/model failure the runtime
surfaces to a chat platform: billing/credit exhaustion, auth/key failures,
rate-limit/quota throttling, and provider outages/connection errors.

Why this module exists
----------------------
Before P17 the gateway mapped raw provider errors to short-but-robotic
notices (``"⚠️ The model provider failed after retries…"``) and, when the
detector missed a billing envelope, leaked the raw body straight to chat —
on 2026-09-04/05 several HQ fleet agents (shepherd, ace, odin on the Grok
spending limit; Linus on a rate limit) posted two back-to-back machine
lines per inbound message, one carrying raw JSON and a ``grok.com`` billing
URL. That reads as a broken machine and violates
``indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages`` (a fleet
agent posts only its final reply, written as a person would write it).

This module is the single place that turns a classified failure into a
first-person, emoji-free line that never contains raw JSON, HTTP status
lines, or vendor URLs, names the owner action, and distinguishes the four
failure classes. A per-conversation cooldown (:class:`ProviderFailureNoticeGate`)
guarantees the same class is said at most once per conversation/thread per
window, so a box whose brain is down never spams a thread and never emits
two notices back to back.

Everything here is pure/text-only so gateway, agent, tests, and the
hq-agents-v2 render test can import it without pulling the gateway graph.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable, Dict, Optional, Tuple

# Default: the same failure class is announced at most once per
# conversation/thread per hour. Overridable via
# ``gateway.provider_failure_notice_cooldown_seconds``.
DEFAULT_PROVIDER_FAILURE_NOTICE_COOLDOWN_SECONDS = 3600

# ── Failure classes ──────────────────────────────────────────────────────────
CLASS_BILLING = "billing"      # credits/subscription exhausted → owner tops up
CLASS_AUTH = "auth"            # key/token invalid or revoked → owner reconnects
CLASS_RATE_LIMIT = "rate_limit"  # transient throttling → back off and retry
CLASS_OUTAGE = "outage"        # provider unreachable / 5xx → wait it out
CLASS_GENERIC = "generic"      # provider failure we couldn't sub-classify

# Fleet-monitor / hq-pro vocabulary for the structured log token. Keep these
# stable — the fleet monitor and the agents-v2 migration preflight grep them.
MONITOR_STATUS_BY_CLASS = {
    CLASS_BILLING: "out-of-credits",
    CLASS_AUTH: "runtime-auth-failed",
    CLASS_RATE_LIMIT: "rate-limited",
    CLASS_OUTAGE: "provider-unreachable",
    CLASS_GENERIC: "provider-failure",
}

# ── Provider display names (owner-facing, never a vendor URL) ─────────────────
_PROVIDER_DISPLAY = {
    "xai": "Grok",
    "xai-oauth": "Grok",
    "grok": "Grok",
    "openai": "Codex",
    "openai-apikey": "Codex",
    "codex": "Codex",
    "anthropic": "Claude",
    "claude": "Claude",
}

# Text signals that identify the provider from a raw error body. Ordered most
# specific first. Values are canonical provider keys for ``_PROVIDER_DISPLAY``.
_PROVIDER_TEXT_SIGNALS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"personal-team-blocked|grok\.com|api\.x\.ai|console\.x\.ai|\bx\.ai\b|\bxai\b|\bgrok\b", re.I), "grok"),
    (re.compile(r"\bcodex\b|openai\.com|api\.openai\.com|\bopenai\b", re.I), "openai"),
    (re.compile(r"anthropic\.com|api\.anthropic\.com|\banthropic\b|\bclaude\b", re.I), "anthropic"),
)


def provider_display_name(provider: Optional[str]) -> Optional[str]:
    """Owner-facing brain name for a provider key/label, or None if unknown."""
    if not provider:
        return None
    key = str(provider).strip().lower()
    if key in _PROVIDER_DISPLAY:
        return _PROVIDER_DISPLAY[key]
    # Accept already-display values ("Grok", "xAI") and unknown providers.
    if key in {"xai", "x.ai"}:
        return "Grok"
    return None


def detect_provider(text: str) -> Optional[str]:
    """Best-effort provider key from a raw error body ('grok', 'openai', …)."""
    if not text:
        return None
    for pattern, key in _PROVIDER_TEXT_SIGNALS:
        if pattern.search(text):
            return key
    return None


# ── Classification ───────────────────────────────────────────────────────────
# Two tiers, tuned so this NEVER rewrites a real assistant answer that merely
# explains an error:
#
#   Tier 1 — DEFINITIVE phrases that essentially never occur in normal prose
#   (a Grok spending-limit code, "billing or credits exhausted", "provider
#   authentication failed"). These classify regardless of length or position.
#   Billing is checked before auth so a spending-limit 403 (also 403/auth- and
#   HTTP-envelope-shaped) is a top-up, not a reconnect.
#
#   Tier 2 — WEAK signals (a bare "rate limit" / "429" / connection error /
#   HTTP status) only classify when the text is shaped like a short provider
#   error ENVELOPE — a marker at the very start of a short body — so a long
#   assistant answer that mentions "HTTP 429" is left untouched.

# Tier 1: definitive billing.
_BILLING_DEFINITIVE_RE = re.compile(
    r"("
    r"personal-team-blocked:spending-limit"
    r"|spending[\s_-]?limit"
    r"|billing or credits exhausted"
    r"|out of credits"
    r"|run out of credits"
    r"|need a grok subscription"
    r"|insufficient[\s_]?(?:quota|credits|funds|balance)"
    r"|no[\s_]?usable[\s_]?credits"
    r"|balance[\s_]?depleted"
    r"|payment[\s_]?required"
    r"|billing[\s_]?not[\s_]?active"
    r"|usage/credit exhaustion"
    r"|credit[\s_]?exhaust"
    r")",
    re.I,
)

# Tier 1: definitive auth.
_AUTH_DEFINITIVE_RE = re.compile(
    r"("
    r"provider authentication failed"
    r"|incorrect api key"
    r"|invalid api key"
    r"|invalid[\s_]?(?:token|credential)"
    r"|authentication[\s_]?(?:failed|error)"
    r"|not logged in"
    r"|not signed in"
    r"|token[\s_]?expired"
    r"|expired[\s_]?token"
    r")",
    re.I,
)

# Tier 2 class signals (matched anywhere, but only inside an envelope-shaped
# body — see ``_looks_like_error_envelope``).
_RATE_LIMIT_RE = re.compile(
    r"("
    r"rate[\s_-]?limit"
    r"|too many requests"
    r"|\b429\b"
    r"|\bquota\b"
    r"|usage limit"
    r"|throttl"
    r")",
    re.I,
)

_OUTAGE_RE = re.compile(
    r"("
    r"connection\s*(?:error|timeout|refused|reset|aborted)"
    r"|connect\s*(?:error|timeout)"
    r"|all connection attempts failed"
    r"|actively refused"
    r"|no route to host"
    r"|network is unreachable"
    r"|cannot connect"
    r"|failed to establish"
    r"|could not connect"
    r"|not responding"
    r"|is not running"
    r"|unreachable"
    r"|overloaded|at capacity|over capacity"
    r"|\b5\d{2}\b"
    r"|service unavailable"
    r"|bad gateway"
    r"|gateway timeout"
    r")",
    re.I,
)

_BILLING_WEAK_RE = re.compile(r"\b402\b", re.I)
_AUTH_WEAK_RE = re.compile(r"\b401\b|\bunauthorized\b|\b403\b", re.I)

# ── Explicit error TOKEN (P17.1) ─────────────────────────────────────────────
# A body with NO error token is NEVER a provider failure, no matter its shape:
# a short numeric answer like "835 files." / "500 widgets." / "404 rows in the
# export." must pass straight through untouched. This regex is BOTH the
# fast-path reject (no token ⇒ return None immediately) and the floor for a
# GENERIC verdict (an envelope shape ALONE never classifies — an explicit token
# must co-occur). It is deliberately a superset of every Tier-2 signal plus the
# HTTP status texts, so a real error string always trips it while a bare number
# never does.
_ERROR_TOKEN_RE = re.compile(
    r"("
    # HTTP status texts (the words that accompany a real status code)
    r"too many requests|unauthorized|forbidden|payment required"
    r"|internal server error|service unavailable|bad gateway|gateway timeout"
    r"|not found|request timeout|precondition failed"
    # generic error vocabulary
    r"|error|failed|failure|exception|traceback|fault|crash"
    # billing / entitlement
    r"|limit|quota|credits?|billing|spending|subscription|balance"
    r"|depleted|insufficient|funds|exhaust"
    # auth
    r"|\bauth\b|authentication|unauthenticated|credential|api[\s_-]?key"
    r"|\btoken\b|logged in|signed in|expired|revoked"
    # throttling / transport / outage
    r"|throttl|rate[\s_-]?limit|timeout|timed out|refused|reset"
    r"|unreachable|overloaded|capacity|not responding|not running"
    r"|could not connect|cannot connect|connection|no route"
    r"|network is unreachable|non-retryable|retries|retry"
    r")",
    re.I,
)

# Start-anchored provider-error envelope shape: an optional run of leading
# non-word/emoji/quote characters, then a marker. Mirrors the pre-P17 gateway
# ``_GATEWAY_PROVIDER_ERROR_SHAPE_RE`` and adds the weak-class starts so raw
# lines like "429 Too Many Requests" / "Rate limit exceeded" / "Connection
# refused" are recognized while assistant prose ("Sure — a 429 means…") is not.
_ENVELOPE_ANCHORED_RE = re.compile(
    r"^[\W\s]*(?:[\w.]*\.)?("  # allow a leading module path, e.g. "httpx."
    r"api\s+(?:call\s+)?failed"
    r"|provider\s+authentication\s+failed"
    r"|non-retryable\s+error"
    r"|rate[\s_-]?limit"
    r"|rate limited after \d+ retries"
    r"|too many requests"
    r"|quota"
    r"|throttl"
    r"|billing or credits exhausted"
    r"|error code\s*:"
    r"|http\s*\d{3}\b"
    r"|[45]\d{2}\s"  # bare leading HTTP ERROR code only: "429 " / "503 ".
    # A real 4xx/5xx, never a plain number like "835 files." (P17.1).
    r"|\{[\"']code[\"']"  # raw JSON error body
    r"|connection\s*(?:error|timeout|refused|reset|aborted)"
    r"|connect\s*(?:error|timeout)"
    r"|could not connect|cannot connect|failed to establish"
    r"|service unavailable|bad gateway|gateway timeout"
    r"|overloaded|unreachable"
    # Our own pre-P17 robotic notices — recompose them idempotently if one ever
    # reaches this composer (e.g. via a legacy/plugin delivery path).
    r"|the model (?:provider|server)"
    r")",
    re.I,
)


def _looks_like_error_envelope(body: str) -> bool:
    """True when ``body`` is shaped like a short provider error envelope.

    Real provider errors are 1-4 short lines beginning with the failure marker;
    assistant answers that discuss an error are longer and start with prose.
    """
    if len(body) > 400 or body.count("\n") > 6:
        return False
    return bool(_ENVELOPE_ANCHORED_RE.search(body))


def classify_failure_text(text: str) -> Optional[str]:
    """Classify a raw error body into a failure class, or None if not one.

    Returns None for ordinary assistant prose (even prose that mentions "rate
    limit" or an HTTP code) — a match needs a Tier-1 definitive phrase or an
    envelope-shaped body carrying a Tier-2 signal.
    """
    if not text:
        return None
    body = str(text).strip()
    if not body:
        return None

    # Tier 1 — definitive, position-independent. These phrases essentially
    # never occur in normal prose, so they classify regardless of shape or the
    # fast-path token floor below (they inherently carry their own tokens).
    if _BILLING_DEFINITIVE_RE.search(body):
        return CLASS_BILLING
    if _AUTH_DEFINITIVE_RE.search(body):
        return CLASS_AUTH

    # Fast path (P17.1) — a body with NO explicit error token is never a
    # provider failure. This is what stops a short numeric answer
    # ("835 files.", "500 widgets.", "404 rows in the export.") from ever being
    # mistaken for an error envelope and rewritten. Return None immediately.
    if not _ERROR_TOKEN_RE.search(body):
        return None

    # Tier 2 — a weak signal only classifies inside a short error envelope.
    if not _looks_like_error_envelope(body):
        return None
    if _BILLING_WEAK_RE.search(body):
        return CLASS_BILLING
    if _AUTH_WEAK_RE.search(body):
        return CLASS_AUTH
    if _RATE_LIMIT_RE.search(body):
        return CLASS_RATE_LIMIT
    if _OUTAGE_RE.search(body):
        return CLASS_OUTAGE
    # Envelope-shaped, an explicit error token present (guaranteed by the
    # fast path above), but no specific weak class → GENERIC. NEVER fall
    # through to GENERIC from an envelope match alone (P17.1): the token floor
    # is the hard requirement, so a bare "500 " leading a non-error sentence
    # can never reach here.
    return CLASS_GENERIC


def looks_like_provider_failure(text: str) -> bool:
    """True when ``text`` is a provider/infra failure, not assistant prose."""
    return classify_failure_text(text) is not None


# ── Composition (in-voice, plain, first person) ──────────────────────────────

def compose_provider_failure_notice(
    failure_class: str,
    *,
    provider: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> str:
    """Return the in-voice line for a failure class.

    Plain first person, no emoji, no raw JSON, no HTTP status text, no vendor
    URLs. Billing and auth name the owner action; rate-limit and outage are
    transient and self-resolving so they don't summon the owner.
    """
    owner = (owner_name or "").strip() or "the owner"
    brain = provider_display_name(provider)
    brain_phrase = f"my {brain}" if brain else "my model"

    if failure_class == CLASS_BILLING:
        return (
            f"I can't think right now — {brain_phrase} credits are out, and "
            f"{owner} needs to top them up before I can pick this back up. "
            "I'll get right back to it as soon as that's done."
        )
    if failure_class == CLASS_AUTH:
        return (
            f"I can't reach {brain_phrase} brain right now — my access got "
            f"disconnected, and {owner} needs to reconnect it before I can "
            "help. I'll pick this up as soon as that's sorted."
        )
    if failure_class == CLASS_RATE_LIMIT:
        return (
            f"I'm getting throttled by {brain_phrase} provider at the moment, "
            "so I'm giving it a short break before I try again."
        )
    if failure_class == CLASS_OUTAGE:
        return (
            f"{brain_phrase.capitalize()} brain isn't responding right now — "
            "looks like a problem on their end. I'll keep an eye on it and "
            "pick this up once it's back."
        )
    # generic
    return (
        f"I'm having trouble reaching {brain_phrase} brain right now. "
        f"{owner} may need to take a look. I'll pick this back up as soon as "
        "it's working again."
    )


def compose_from_text(
    text: str,
    *,
    provider: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> Optional[str]:
    """Classify ``text`` and return the composed notice, or None if not a
    provider failure. Provider is detected from the body when not supplied."""
    failure_class = classify_failure_text(text)
    if failure_class is None:
        return None
    resolved_provider = provider or detect_provider(text)
    return compose_provider_failure_notice(
        failure_class, provider=resolved_provider, owner_name=owner_name
    )


# ── Structured log line (fleet monitor / hq-pro classify off this) ───────────

def structured_log_line(
    failure_class: str,
    provider: Optional[str],
    *,
    conversation: Optional[str] = None,
) -> str:
    """``provider_failure class=billing provider=grok status=out-of-credits …``.

    Stable, greppable, one line. The fleet monitor and the agents-v2 migration
    preflight parse the ``class=``/``provider=``/``status=`` tokens.
    """
    prov = (provider or "unknown").strip() or "unknown"
    status = MONITOR_STATUS_BY_CLASS.get(failure_class, "provider-failure")
    parts = [
        "provider_failure",
        f"class={failure_class}",
        f"provider={prov}",
        f"status={status}",
    ]
    if conversation:
        # Never emit raw chat ids that could be sensitive; the source key is a
        # platform:chat:thread tuple, safe to log for correlation.
        parts.append(f"conversation={conversation}")
    return " ".join(parts)


# ── Per-conversation cooldown gate ───────────────────────────────────────────

class ProviderFailureNoticeGate:
    """Rate-limit provider-failure notices per (conversation, class).

    ``should_emit`` is an atomic check-and-set: the first caller for a given
    (conversation, class) inside the cooldown window gets True (and the window
    starts), every later caller gets False until the window elapses. That
    single primitive both prevents two back-to-back notices in one turn (final
    + status racing) and silences repeat inbound messages while the brain is
    still down.

    A falsy ``conversation_key`` disables rate limiting (always True) so unit
    tests and raw/CLI surfaces that have no conversation identity still compose.
    """

    def __init__(
        self,
        cooldown_seconds: float = DEFAULT_PROVIDER_FAILURE_NOTICE_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown = float(cooldown_seconds)
        self._clock = clock
        self._last: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown

    def set_cooldown(self, cooldown_seconds: float) -> None:
        self._cooldown = float(cooldown_seconds)

    def should_emit(
        self,
        conversation_key: Optional[str],
        failure_class: str,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """True if a notice for this (conversation, class) may be sent now.

        Records the emission time when it returns True. Cooldown <= 0 disables
        rate limiting (every call emits) — matching an operator who sets the
        cooldown to 0 to opt out.
        """
        if not conversation_key:
            return True
        if self._cooldown <= 0:
            return True
        ts = self._clock() if now is None else now
        key = (conversation_key, failure_class)
        with self._lock:
            last = self._last.get(key)
            if last is not None and (ts - last) < self._cooldown:
                return False
            self._last[key] = ts
            return True

    def reset(self, conversation_key: Optional[str] = None) -> None:
        """Clear all cooldown state, or just one conversation's."""
        with self._lock:
            if conversation_key is None:
                self._last.clear()
                return
            for key in [k for k in self._last if k[0] == conversation_key]:
                self._last.pop(key, None)
