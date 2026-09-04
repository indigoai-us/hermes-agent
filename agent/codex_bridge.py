"""HQ approval bridge contract for the codex app-server transport (fork patch
P7 / story A10-CODEX-ENFORCEMENT-GAP).

The codex ``app-server`` transport hands a turn to a ``codex`` subprocess whose
native shell / ``apply_patch`` tools never reach Hermes' ``pre_tool_call`` hook,
so on its own it bypasses HQ enforcement (proven live 2026-09-02). It is only
safe when the HQ approval bridge is active: codex is never auto-approved, and
sandbox / approvalPolicy are pinned into ``thread/start`` so codex raises an
approval request per shell / apply_patch action — each of which is routed
through ``hooks/hq-agents-v2-hook-adapter.sh pre_approval_request`` (hook allow
=> approve, hook deny / adapter error / timeout => deny).

LOCKSTEP NOTE — this module is an EXACT MIRROR of
``provision/codex_bridge.py`` in ``indigoai-us/hq-agents-v2`` (the citizenship
package). That repo is the canonical, unit-tested source of truth for the
contract (``provision/tests/test_codex_bridge.py``); this copy exists only so
the fork runtime can import a dependency-free contract without vendoring the
citizenship package. The two files MUST stay byte-for-byte identical below the
header. Any change to the contract (allowed sandbox / approval-policy values,
the thread/start param shape, the routing / validation rules) must land in BOTH
files in the same change, and ``tests/agent/test_codex_bridge.py`` here mirrors
the hq-agents-v2 suite so a drift trips a test on the next pin bump.

This module is the single source of truth for THAT contract:

* ``thread_start_params`` — what the fork sends on ``thread/start`` (the pinned
  ``sandbox`` + ``approvalPolicy``), grounded in
  ``agent/transports/codex_app_server_session.py`` which without the bridge
  sends only ``{"cwd": ...}``.
* ``resolve_routing`` — the ``_ServerRequestRouting`` auto-approve flags; when
  the bridge is active they are ALWAYS off, even if the box's
  ``approvals.mode`` bypass (``is_approval_bypass_active()``) is on. This is the
  "stop auto-approving codex" requirement.
* ``validate`` — fail closed unless the config is a safe bridge shape.

Pure and dependency-free so it is safe to import inside the runtime and testable
without the runtime.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Valid codex values (codex-rs app-server protocol v2). ``never`` is a real
# codex approval policy but it defeats the bridge (codex would never ask), so it
# is intentionally NOT accepted here.
SANDBOX_ALLOWED = frozenset({"read-only", "workspace-write", "danger-full-access"})
APPROVAL_POLICY_ALLOWED = frozenset({"untrusted", "on-failure", "on-request"})

# codex ``thread/start`` params use camelCase; the hermes config uses snake_case.
_APPROVAL_POLICY_WIRE = "approvalPolicy"
_SANDBOX_WIRE = "sandbox"


class BridgeConfigError(ValueError):
    """Raised when a codex_app_server config is not a safe bridge shape.

    Callers MUST treat this as fail-closed: refuse to start app-server / refuse
    to render, never fall back to a permissive default.
    """


def _model(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return ((cfg or {}).get("model") or {}) if isinstance(cfg, dict) else {}


def _cas(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    val = (cfg or {}).get("codex_app_server") if isinstance(cfg, dict) else None
    return val if isinstance(val, dict) else {}


def app_server_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    """True iff model.openai_runtime opts into the codex app-server transport."""
    return _model(cfg).get("openai_runtime") == "codex_app_server"


def bridge_active(cfg: Optional[Dict[str, Any]]) -> bool:
    """True iff app-server is on AND the HQ approval bridge is explicitly on."""
    return app_server_enabled(cfg) and _cas(cfg).get("approval_bridge") is True


def validate(cfg: Optional[Dict[str, Any]]) -> None:
    """Fail closed unless the config is safe.

    No app-server => nothing to check. App-server on => the bridge must be on,
    auto_approve must be exactly False, and sandbox + approval_policy must be
    pinned to accepted values. Anything else raises ``BridgeConfigError``.
    """
    if not app_server_enabled(cfg):
        return
    cas = _cas(cfg)
    if cas.get("approval_bridge") is not True:
        raise BridgeConfigError(
            "codex_app_server requires approval_bridge: true (it bypasses "
            "pre_tool_call enforcement otherwise)"
        )
    # Must be explicitly False — a missing/None/truthy value fails closed.
    if cas.get("auto_approve", None) is not False:
        raise BridgeConfigError(
            "codex_app_server requires auto_approve: false (never bypass the hook)"
        )
    sandbox = cas.get("sandbox")
    if sandbox not in SANDBOX_ALLOWED:
        raise BridgeConfigError(
            f"codex_app_server.sandbox must be one of {sorted(SANDBOX_ALLOWED)} "
            f"(got {sandbox!r})"
        )
    policy = cas.get("approval_policy")
    if policy not in APPROVAL_POLICY_ALLOWED:
        raise BridgeConfigError(
            f"codex_app_server.approval_policy must be one of "
            f"{sorted(APPROVAL_POLICY_ALLOWED)} (got {policy!r}) — 'never' is "
            "refused because it defeats the bridge"
        )


def thread_start_params(cfg: Optional[Dict[str, Any]], *, cwd: str) -> Dict[str, Any]:
    """Params for the codex ``thread/start`` request.

    Always carries ``cwd`` (as upstream does today). When the bridge is active
    it ALSO pins ``sandbox`` + ``approvalPolicy`` so codex raises an approval
    request per shell / apply_patch action. Fail closed on an unsafe config.
    """
    validate(cfg)
    params: Dict[str, Any] = {"cwd": cwd}
    if bridge_active(cfg):
        cas = _cas(cfg)
        params[_SANDBOX_WIRE] = cas["sandbox"]
        params[_APPROVAL_POLICY_WIRE] = cas["approval_policy"]
    return params


def resolve_routing(
    cfg: Optional[Dict[str, Any]], *, approval_bypass_active: bool
) -> Dict[str, bool]:
    """Auto-approve flags for ``_ServerRequestRouting``.

    When the bridge is active, auto-approve is ALWAYS off — every codex approval
    request must reach the HQ hook adapter, regardless of the box's
    ``approvals.mode`` bypass. When the bridge is not active, preserve stock
    behavior (mirror ``is_approval_bypass_active()``). Fail closed on an unsafe
    config.
    """
    validate(cfg)
    if bridge_active(cfg):
        return {"auto_approve_exec": False, "auto_approve_apply_patch": False}
    return {
        "auto_approve_exec": bool(approval_bypass_active),
        "auto_approve_apply_patch": bool(approval_bypass_active),
    }
