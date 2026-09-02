"""Opt-in hardening flags for the shell-hook dispatch path.

``pre_tool_call`` hooks can already fail closed per hook (``fail_closed`` on
the hook entry) and at the dispatcher timeout layer
(``_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS``). One gap remains: when the DISPATCHER
itself raises — an import failure, a bug in hook plumbing, a poisoned plugin
registry — both tool-execution paths swallow the exception and let the tool
run unblocked. For operators using ``pre_tool_call`` as a policy boundary
(not just telemetry), that silent fail-open defeats the point of the gate.

``agent.hooks_dispatcher_fail_closed: true`` turns a dispatcher failure into
a block whose message names the flag, so the model (and the operator reading
the transcript) sees exactly why the tool was refused. Default OFF to
preserve the existing fail-open behavior for everyone else.

Config idiom mirrors :mod:`agent.verify_hooks` (``agent:`` section,
``load_config`` fallback, exception-safe).
"""

from __future__ import annotations

from typing import Any, Optional

from utils import is_truthy_value


def hooks_dispatcher_fail_closed(config: Optional[dict[str, Any]] = None) -> bool:
    """True when a ``pre_tool_call`` dispatcher failure must block the tool."""
    return is_truthy_value(
        _agent_cfg(config).get("hooks_dispatcher_fail_closed", False),
        default=False,
    )


def hooks_shell_reentrant(config: Optional[dict[str, Any]] = None) -> bool:
    """True when shell-hook callbacks may run concurrently.

    The hook dispatcher single-flights every callback: while one fire is in
    progress, a second fire of the same callback is *skipped* — and for
    ``pre_tool_call`` a skip fails closed, so the second tool call is refused
    with "callback timed out or is still running". That guard exists for
    in-process Python callbacks that can hang the loop. A shell hook is one
    subprocess per fire with its own timeout and process-tree kill, so
    concurrent fires are independent; refusing the second one only turns the
    agent's parallel tool batches into spurious blocks. Default OFF keeps the
    stock single-flight for everyone else.
    """
    return is_truthy_value(
        _agent_cfg(config).get("hooks_shell_reentrant", False),
        default=False,
    )


def dispatcher_fail_closed_message(exc: BaseException) -> str:
    """The block message a dispatcher failure produces (becomes the tool result)."""
    return (
        "pre_tool_call hook dispatcher failed; failing closed "
        f"(agent.hooks_dispatcher_fail_closed): {exc}"
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
    "dispatcher_fail_closed_message",
    "hooks_dispatcher_fail_closed",
    "hooks_shell_reentrant",
]
