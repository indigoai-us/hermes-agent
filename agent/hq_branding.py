from __future__ import annotations

import os
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
    "DISPLAY_NAME_ENV",
    "HQ_IDENTITY_FALLBACK",
    "HQ_IDENTITY_LEAD",
    "STOCK_IDENTITY_LEAD",
    "FALLBACK_AGENT_NAME",
    "STOCK_AGENT_NAME",
    "agent_name",
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
