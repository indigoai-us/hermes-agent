"""Opt-in context forwarding for the codex app-server runtime.

The codex app-server path hands the whole turn to a ``codex app-server``
subprocess: Hermes builds its system prompt (SOUL.md, project context
files, plugin sections) and composes per-turn plugin/memory context for
the user message — and then sends codex only the raw user text. Codex
runs on its own base instructions plus whatever AGENTS.md sits in the
thread cwd, so every Hermes-side context seam silently no-ops for
operators who picked the codex brain.

Two flags close the gap, both default OFF (existing behavior unchanged):

``agent.codex_forward_system_prompt: true``
    Send Hermes' composed system prompt as ``developerInstructions`` on
    ``thread/start``. Codex keeps its own base instructions (unlike
    ``baseInstructions``, which would replace them and break codex's
    tool-calling conventions); the Hermes context arrives as the
    developer message — the documented slot for exactly this.
    Supported by the codex app-server protocol v2 (verified against
    codex-cli 0.152 ``generate-json-schema``: ThreadStartParams accepts
    ``developerInstructions``).

``agent.codex_forward_plugin_context: true``
    Compose the turn input the same way the standard loop composes the
    API copy of the user message (``compose_user_api_content``:
    memory-prefetch context + ``pre_llm_call`` plugin context), instead
    of sending the raw user text.

Config idiom mirrors :mod:`agent.hook_flags` (``agent:`` section,
``load_config`` fallback, exception-safe).
"""

from __future__ import annotations

from typing import Any, Optional

from utils import is_truthy_value


def codex_forward_system_prompt(config: Optional[dict[str, Any]] = None) -> bool:
    """True when thread/start must carry the Hermes system prompt."""
    return is_truthy_value(
        _agent_cfg(config).get("codex_forward_system_prompt", False),
        default=False,
    )


def codex_forward_plugin_context(config: Optional[dict[str, Any]] = None) -> bool:
    """True when the codex turn input must include composed plugin context."""
    return is_truthy_value(
        _agent_cfg(config).get("codex_forward_plugin_context", False),
        default=False,
    )


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
    "codex_forward_plugin_context",
    "codex_forward_system_prompt",
]
