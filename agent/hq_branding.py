from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Optional

from utils import is_truthy_value

FALLBACK_AGENT_NAME = "HQ"
STOCK_AGENT_NAME = "Hermes"
DISPLAY_NAME_ENV = "HQ_AGENT_DISPLAY_NAME"


def enabled(config: Optional[dict[str, Any]] = None) -> bool:
    if config is not None:
        return is_truthy_value(_agent_cfg(config).get("hq_branding", False), default=False)
    return _enabled_from_loaded_config()


def agent_name(config: Optional[dict[str, Any]] = None) -> str:
    if not enabled(config):
        return STOCK_AGENT_NAME
    return os.environ.get(DISPLAY_NAME_ENV, "").strip() or FALLBACK_AGENT_NAME


def interrupt_notice(restarting: bool, config: Optional[dict[str, Any]] = None) -> str:
    if not enabled(config):
        action = "restarting" if restarting else "shutting down"
        hint = (
            "Your current task will be interrupted. "
            "Send any message after restart and I'll try to resume where you left off."
            if restarting
            else "Your current task will be interrupted."
        )
        return f"⚠️ Gateway {action} — {hint}"
    name = agent_name(config)
    if restarting:
        return (
            f"⚠️ {name} is restarting — your current task will stop. "
            "Send me anything once I'm back and I'll try to pick up where we left off."
        )
    return f"⚠️ {name} is offline for a moment — your current task will stop here."


def busy_notice(
    restarting: bool,
    queued: bool = False,
    new_work: bool = False,
    config: Optional[dict[str, Any]] = None,
) -> str:
    gerund = "restarting" if restarting else "shutting down"
    if not enabled(config):
        if queued:
            return f"⏳ Gateway {gerund} — queued for the next turn after it comes back."
        scope = "new work" if new_work else "another turn"
        return f"⏳ Gateway is {gerund} and is not accepting {scope} right now."
    subject = f"{agent_name(config)} is " + ("restarting" if restarting else "going offline")
    if queued:
        return f"⏳ {subject} — I've queued this and I'll answer once I'm back."
    if new_work:
        return f"⏳ {subject} — I can't take new work right now."
    return f"⏳ {subject} — give me a moment."


def back_online_notice(config: Optional[dict[str, Any]] = None) -> str:
    if not enabled(config):
        return "♻️ Gateway online — Hermes is back and ready."
    return f"♻️ {agent_name(config)} is back online and ready."


def restarted_notice(config: Optional[dict[str, Any]] = None) -> str:
    if not enabled(config):
        return "♻ Gateway restarted successfully. Your session continues."
    return f"♻ {agent_name(config)} is back — picking up where we left off."


def cron_interrupt_notice(
    job_label: str, restarting: bool, config: Optional[dict[str, Any]] = None
) -> str:
    if not enabled(config):
        action = "restarting" if restarting else "shutting down"
        return (
            f"⚠️ Cron job '{job_label}' was interrupted — "
            f"the gateway is {action} and killed the run before it "
            "finished. No result was produced for this run."
        )
    return (
        f"⚠️ Scheduled job '{job_label}' didn't finish — "
        f"{agent_name(config)} went offline mid-run. "
        "Nothing was produced for this run."
    )


def update_target_label(config: Optional[dict[str, Any]] = None) -> str:
    return agent_name(config) if enabled(config) else "Hermes Agent"


def update_finished_notice(
    successfully: bool = False, config: Optional[dict[str, Any]] = None
) -> str:
    if not enabled(config):
        return (
            "✅ Hermes update finished successfully."
            if successfully
            else "✅ Hermes update finished."
        )
    return f"✅ {agent_name(config)} finished updating."


def update_failed_notice(
    exit_code: Optional[int] = None,
    hint: bool = False,
    config: Optional[dict[str, Any]] = None,
) -> str:
    if not enabled(config):
        if exit_code is not None:
            return "❌ Hermes update failed (exit code {}).".format(exit_code)
        if hint:
            return (
                "❌ Hermes update failed. Check the gateway logs or run "
                "`hermes update` manually for details."
            )
        return "❌ Hermes update failed."
    name = agent_name(config)
    if exit_code is not None:
        return f"❌ {name} could not update (exit code {exit_code})."
    if hint:
        return (
            f"❌ {name} could not update. Check the logs or run "
            "`hermes update` manually for details."
        )
    return f"❌ {name} could not update."


def update_timeout_notice(config: Optional[dict[str, Any]] = None) -> str:
    if not enabled(config):
        return "❌ Hermes update timed out after 30 minutes."
    return f"❌ {agent_name(config)} timed out updating after 30 minutes."


STOCK_IDENTITY_LEAD = "You are Hermes Agent, built by Nous Research. "
HQ_IDENTITY_LEAD = "You are an HQ agent, a teammate inside your company's HQ workspace. "
HQ_IDENTITY_FALLBACK = (
    HQ_IDENTITY_LEAD
    + "Be direct: match the length of your reply to the weight of the ask. "
    "No filler, no restating the request back, no narrating tool calls the "
    "user can see. Plain claims over adjectives; when unsure, say so plainly. "
    "Agree because it's right, not because the user said it."
)


def default_agent_identity(stock: str, config: Optional[dict[str, Any]] = None) -> str:
    if not enabled(config):
        return stock
    if stock.startswith(STOCK_IDENTITY_LEAD):
        return HQ_IDENTITY_LEAD + stock[len(STOCK_IDENTITY_LEAD):]
    return HQ_IDENTITY_FALLBACK


def help_guidance(stock: str, config: Optional[dict[str, Any]] = None) -> str:
    return "" if enabled(config) else stock


@lru_cache(maxsize=1)
def _enabled_from_loaded_config() -> bool:
    return is_truthy_value(_agent_cfg(None).get("hq_branding", False), default=False)


# ── Fork patch P14 (hq/v2): approval voice ───────────────────────────────────
# The runtime's command-approval prompt reads as a person, not a machine. When
# ``gateway.approval_voice_enabled`` is on (HQ boxes render it true; default off
# reproduces the stock ":warning: Command Approval Required" banner), an approval
# ask is one or two plain lines addressed to the requester describing what the
# agent needs to do and why. The raw command never appears in the message body —
# it goes behind a fold / thread-reply "details:" block. The confirmation drops
# the robotic ":white_check_mark: Approved for session by <handle>"; on deny/
# timeout the agent says it is skipping the step, in voice. All of the
# user-facing approval copy lives here so a later change cannot re-introduce a
# robotic banner elsewhere (source tripwire in tests/agent/test_hq_branding.py).

APPROVAL_DETAILS_PREFIX = "details:"


def approval_voice_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    """Fork patch P14: honor ``gateway.approval_voice_enabled``.

    Default False reproduces stock upstream approval prompts. HQ boxes render it
    true via the hq-agents-v2 config template. A config-load failure fails
    closed to False (stock) so a broken config never silently changes wording.
    """
    if config is not None and isinstance(config, dict):
        gw = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
        if "approval_voice_enabled" in config:
            return is_truthy_value(config.get("approval_voice_enabled"), default=False)
        if isinstance(gw, dict) and "approval_voice_enabled" in gw:
            return is_truthy_value(gw.get("approval_voice_enabled"), default=False)
        return False
    try:
        from gateway.config import load_gateway_config

        return bool(getattr(load_gateway_config(), "approval_voice_enabled", False))
    except Exception:
        return False


_GENERIC_INTENT = "run a quick command on my box"


# ── Fork patch P14.3: forbidden literals for any chat-surface approval text ───
# Deacon (indigo, v2.25) folded an ``execute_code``/``hermes_tools`` heredoc —
# with COGNITO secret NAMES and a ``/tmp`` path — into a shared-channel thread
# reply (2026-09-06). Even redacted of secret *values*, runtime/tool internals,
# secret variable *names*, and file paths must never reach a Slack (or any
# non-HQ-DM) surface. These are the tripwire literals: the raw folded command
# is visible only in the owner's HQ DM. Policy:
# indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages.
_APPROVAL_JARGON_RE = re.compile(
    r"\b(?:hermes_tools|execute_code|code_execution|hermes_state|hermes_cli)\b"
    r"|\bterminal\s*\("
    r"|\bfrom\s+hermes_tools\b",
    re.IGNORECASE,
)
# Environment-variable style secret NAMES: FOO_PASSWORD, FOO_API_TOKEN,
# AWS_SECRET_ACCESS_KEY, *_SECRET, *_KEY, etc. (names, not values).
_APPROVAL_SECRET_NAME_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*"
    r"_(?:PASSWORD|PASSWD|PWD|TOKEN|KEY|SECRET|CREDENTIAL|CREDENTIALS|APIKEY|USERNAME)\b"
)
# Absolute / home-relative file paths (``/tmp/...``, ``/home/...``, ``~/...``,
# or any multi-segment absolute path).
_APPROVAL_PATH_RE = re.compile(
    r"(?<![\w./-])(?:~|/(?:tmp|home|Users|var|etc|opt|root|mnt|srv|usr|private))"
    r"/[^\s\"'`)]+"
    r"|(?<![\w./-])/(?:[A-Za-z0-9._-]+/){2,}[A-Za-z0-9._-]+"
)


def contains_forbidden_approval_literal(text: str) -> Optional[str]:
    """Return the first runtime/secret-name/path literal in ``text``, else None.

    The Slack approval path asserts this returns ``None`` for every message it
    puts on a chat surface (tripwire). Used both as a test oracle and a runtime
    guard so a later change cannot re-introduce a leak (P14.3).
    """
    s = str(text or "")
    for rx in (_APPROVAL_JARGON_RE, _APPROVAL_SECRET_NAME_RE, _APPROVAL_PATH_RE):
        m = rx.search(s)
        if m:
            return m.group(0)
    return None


def redact_approval_details(command: str) -> str:
    """Redact a command for any approval *details* text that could be shown.

    Removes secret *values* (Tirith-grade, fail-soft) AND the P14.3 forbidden
    literals — runtime/tool internals (``hermes_tools``, ``execute_code``,
    ``terminal(``), secret variable *names* (``*_PASSWORD``/``*_TOKEN``/
    ``*_KEY``/``*_SECRET``), and file paths — so the result never carries
    implementation detail onto a chat surface. The unredacted folded command
    is delivered only to the owner's HQ DM by the HQ DM path.
    """
    text = str(command or "")
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:
        # Fail-soft on the value redactor; the literal passes below still run.
        pass
    text = _APPROVAL_JARGON_RE.sub("[tool]", text)
    text = _APPROVAL_SECRET_NAME_RE.sub("[secret]", text)
    text = _APPROVAL_PATH_RE.sub("[path]", text)
    return text


# Fork patch P14.2: tokens that mark a "description" as runtime/shell jargon or
# a raw command fragment, never a human intent. The upstream approval metadata
# often carries a machine phrase like "script execution via -e/-c flag" (live
# 2026-09 lilo-social/Stitch), and a passthrough of it read as robotic and
# leaked implementation detail into a customer channel. Any hit ⇒ fall back to
# the generic phrase rather than surface the description verbatim.
_JARGON_DESC_MARKERS = (
    "-e/-c",
    "-c flag",
    "-e flag",
    "script execution",
    "execute script",
    "arbitrary code",
    "code execution",
    "os.environ",
    "environ",
    "printenv",
    "subprocess",
    "eval(",
    "exec(",
    "import ",
    "$(",
    "```",
    "python3 -c",
    "python -c",
)


# Interpreter inline-script execution (python/node/ruby/perl -c|-e, or the
# bash|sh -c|-lc wrapper). Classified to a generic phrase so the ask never
# echoes the flag jargon or the script body.
_INLINE_SCRIPT_RE = re.compile(
    r"(^|/)(python[0-9.]*|node|ruby|perl)\b[^\n]*\s-(?:c|e)\b"
    r"|(^|/)(ba)?sh\b[^\n]*\s-[a-z]*c\b"
)


def _intent_desc_is_safe(desc: str, command: str) -> bool:
    """True when a description reads as a human phrase, not jargon or a command.

    Rejects a description that carries shell-flag jargon, code-shaped tokens, or
    any run of the raw command — so the ask never surfaces implementation detail
    (P14.2). Conservative: when in doubt, the caller uses the generic phrase.
    """
    d = (desc or "").strip()
    if not d:
        return False
    low = d.lower()
    if low in ("dangerous command", "command", "shell command"):
        return False
    # P14.3: a description that names a secret var, a tool internal, or a file
    # path is a leak even without shell punctuation — route to the generic
    # phrase instead of echoing it into the ask.
    if contains_forbidden_approval_literal(d):
        return False
    for marker in _JARGON_DESC_MARKERS:
        if marker in low:
            return False
    # A description that quotes (any 12+ char run of) the raw command is a leak.
    cmd = (command or "").strip()
    if cmd and len(cmd) >= 12 and cmd[:12].lower() in low:
        return False
    # Code punctuation density is a strong tell for a command masquerading as a
    # description (braces, pipes, redirects, semicolons, backticks).
    if any(ch in d for ch in "{}|;`") or "&&" in d:
        return False
    return True


def summarize_command_intent(command: str, description: str = "") -> str:
    """Paraphrase a pending action into a short human phrase.

    The raw command is never surfaced; this classifies the command so the ask
    reads like a person ("read my instance details", "list some files on my
    box", "call the AWS API"). Falls back to a description ONLY when it reads as
    a human phrase (see :func:`_intent_desc_is_safe`), otherwise a generic
    phrase. Used only to build the ask body, never to display the command.
    """
    cmd = (command or "").strip()
    low = cmd.lower()
    if not cmd:
        desc = (description or "").strip()
        return desc if _intent_desc_is_safe(desc, cmd) else _GENERIC_INTENT
    # Cloud instance metadata (the 2026-09-04 EC2-cost case).
    if "169.254.169.254" in low or "meta-data" in low or "instance-id" in low:
        return "read my instance details"
    # AWS API / cloud control-plane calls.
    if low.startswith("aws ") or "amazonaws.com" in low or " aws " in f" {low} ":
        return "call the AWS API"
    # Inline-script / interpreter -c/-e execution (the 2026-09 python3 -c env
    # dump). Classify to a plain phrase — never echo the "-e/-c flag" jargon or
    # the script body into the ask. Covers python/node/ruby/perl -c|-e and the
    # bash|sh -c|-lc wrapper.
    if _INLINE_SCRIPT_RE.search(low):
        return _GENERIC_INTENT
    # Directory listings.
    if low.startswith("ls ") or low == "ls" or low.startswith("find "):
        return "list some files on my box"
    # Reads.
    if low.startswith(("cat ", "head ", "tail ", "less ", "grep ")):
        return "read a file on my box"
    # Git.
    if low.startswith("git "):
        return "run a git command"
    # HTTP fetch.
    if low.startswith(("curl ", "wget ", "http ")):
        return "make a network request from my box"
    desc = (description or "").strip()
    if _intent_desc_is_safe(desc, cmd):
        return desc
    return _GENERIC_INTENT


def approval_ask_text(requester_name: Optional[str], intent: str) -> str:
    """One human line asking the requester for the OK to run a pending action."""
    intent = (intent or "run a quick command on my box").strip()
    name = (requester_name or "").strip()
    if name:
        return f"{name}, to answer that I need to {intent}. OK to go ahead?"
    return f"To answer that I need to {intent}. OK to go ahead?"


def approval_details_block(command: str) -> str:
    """The command folded behind a details reply, redacted for a chat surface.

    Fork patch P14.3: the folded command is redacted of secret *values*,
    runtime/tool internals (``hermes_tools``/``execute_code``/``terminal(``),
    secret variable *names*, and file paths (:func:`redact_approval_details`),
    so it never carries implementation detail even when routed to the owner's
    private DM. The unredacted folded command is delivered only to the owner's
    HQ DM by the HQ DM path.

    It is NEVER appended to a buttonless chat body, and never to a channel,
    group, or channel thread — on a surface with no fold the "details:" line
    renders inline (odin, 2026-09-06), and on a shared channel every member can
    expand a thread reply (Deacon, 2026-09-06). The buttonless text fallback
    uses :func:`approval_reply_hint`; the Slack path only ever sends this to an
    owner/admin DM after the :func:`contains_forbidden_approval_literal`
    tripwire passes.
    """
    cmd = redact_approval_details(command).strip()
    return f"{APPROVAL_DETAILS_PREFIX}\n```\n{cmd}\n```"


def approval_reply_hint(
    allow_session: bool = True, allow_permanent: bool = True
) -> str:
    """Fork patch P14.1: a plain, scaffold-free reply hint for the buttonless
    approval ask.

    HQ fleet agents must never post the command-approval scaffold — the
    ``/approve`` / ``/approve session`` / ``/approve always`` / ``/deny``
    instruction block — or the raw command into a chat surface. On HQ DM that
    scaffold leaked verbatim into a user DM (odin, 2026-09-06; policy
    indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages). The
    approval GATE is unchanged: a plain "yes" / "no" reply resolves it through
    the gateway's has_blocking_approval intercept, and HQ DM additionally
    renders a clickable ``decision`` block (hqdm send_exec_approval, #40). This
    hint carries NO slash command, NO raw command, and NO banner — only words a
    person would use, each of which the intercept understands.
    """
    hint = "Reply *yes* to go ahead or *no* to skip."
    if allow_session and allow_permanent:
        hint += " Say *always* if you'd rather I stop asking for this."
    return hint


def approval_confirmation_text() -> str:
    """Plain in-voice confirmation shown after an approval (never a handle)."""
    return "Thanks, running it."


def approval_denied_text() -> str:
    """In-voice line when the approval is denied or times out."""
    return "Skipping that check since I didn't get an OK; here's what I can say without it."


def _agent_cfg(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    agent_cfg = (config or {}).get("agent") if isinstance(config, dict) else None
    return agent_cfg if isinstance(agent_cfg, dict) else {}


__all__ = [
    "APPROVAL_DETAILS_PREFIX",
    "DISPLAY_NAME_ENV",
    "HQ_IDENTITY_FALLBACK",
    "HQ_IDENTITY_LEAD",
    "STOCK_IDENTITY_LEAD",
    "FALLBACK_AGENT_NAME",
    "STOCK_AGENT_NAME",
    "agent_name",
    "approval_ask_text",
    "approval_confirmation_text",
    "approval_denied_text",
    "approval_details_block",
    "approval_reply_hint",
    "approval_voice_enabled",
    "contains_forbidden_approval_literal",
    "redact_approval_details",
    "summarize_command_intent",
    "back_online_notice",
    "busy_notice",
    "cron_interrupt_notice",
    "default_agent_identity",
    "enabled",
    "help_guidance",
    "interrupt_notice",
    "restarted_notice",
    "update_failed_notice",
    "update_finished_notice",
    "update_target_label",
    "update_timeout_notice",
]
