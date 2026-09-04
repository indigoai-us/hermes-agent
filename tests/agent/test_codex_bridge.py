"""Unit tests for the A10 codex app-server approval-bridge contract
(agent/codex_bridge.py). Pins what fork patch P7 must do: pin sandbox +
approvalPolicy into thread/start and never auto-approve when the bridge is on.

This mirrors ``provision/tests/test_codex_bridge.py`` in
``indigoai-us/hq-agents-v2`` (which tests the canonical copy of the contract).
agent/codex_bridge.py is a byte-for-byte mirror of that repo's
provision/codex_bridge.py; keeping both suites green on each pin bump is the
lockstep tripwire.
"""

import pytest

import agent.codex_bridge as codex_bridge


def _cfg(**cas_overrides):
    """A safe bridge config; override the codex_app_server sub-keys per test."""
    cas = {
        "approval_bridge": True,
        "auto_approve": False,
        "sandbox": "workspace-write",
        "approval_policy": "untrusted",
    }
    cas.update(cas_overrides)
    return {"model": {"provider": "openai-codex", "openai_runtime": "codex_app_server"},
            "codex_app_server": cas}


# ── enablement / activation ──────────────────────────────────────────────────

def test_app_server_off_by_default():
    assert codex_bridge.app_server_enabled({"model": {"provider": "openai-codex"}}) is False
    assert codex_bridge.bridge_active({"model": {"provider": "openai-codex"}}) is False


def test_app_server_on_requires_bridge_flag_for_activation():
    cfg = {"model": {"openai_runtime": "codex_app_server"}}  # no codex_app_server block
    assert codex_bridge.app_server_enabled(cfg) is True
    assert codex_bridge.bridge_active(cfg) is False


def test_bridge_active_when_fully_configured():
    assert codex_bridge.bridge_active(_cfg()) is True


# ── thread/start pins sandbox + approvalPolicy ───────────────────────────────

def test_thread_start_pins_sandbox_and_approval_policy():
    params = codex_bridge.thread_start_params(_cfg(), cwd="/hq")
    assert params["cwd"] == "/hq"
    assert params["sandbox"] == "workspace-write"
    assert params["approvalPolicy"] == "untrusted"  # camelCase wire key


def test_thread_start_respects_pinned_values():
    params = codex_bridge.thread_start_params(
        _cfg(sandbox="read-only", approval_policy="on-request"), cwd="/x")
    assert params["sandbox"] == "read-only"
    assert params["approvalPolicy"] == "on-request"


def test_thread_start_no_app_server_is_cwd_only():
    params = codex_bridge.thread_start_params({"model": {"provider": "openai-codex"}}, cwd="/hq")
    assert params == {"cwd": "/hq"}


# ── stop auto-approving ──────────────────────────────────────────────────────

def test_routing_forces_no_auto_approve_when_bridge_active_even_if_bypass_on():
    routing = codex_bridge.resolve_routing(_cfg(), approval_bypass_active=True)
    assert routing == {"auto_approve_exec": False, "auto_approve_apply_patch": False}


def test_routing_preserves_stock_behavior_without_app_server():
    cfg = {"model": {"provider": "openai-codex"}}
    assert codex_bridge.resolve_routing(cfg, approval_bypass_active=True) == {
        "auto_approve_exec": True, "auto_approve_apply_patch": True}
    assert codex_bridge.resolve_routing(cfg, approval_bypass_active=False) == {
        "auto_approve_exec": False, "auto_approve_apply_patch": False}


# ── fail-closed validation ───────────────────────────────────────────────────

def test_validate_ok_for_safe_config():
    codex_bridge.validate(_cfg())  # no raise


def test_app_server_without_bridge_fails_closed():
    cfg = {"model": {"openai_runtime": "codex_app_server"}}
    with pytest.raises(codex_bridge.BridgeConfigError):
        codex_bridge.validate(cfg)
    with pytest.raises(codex_bridge.BridgeConfigError):
        codex_bridge.thread_start_params(cfg, cwd="/x")
    with pytest.raises(codex_bridge.BridgeConfigError):
        codex_bridge.resolve_routing(cfg, approval_bypass_active=False)


def test_auto_approve_true_fails_closed():
    with pytest.raises(codex_bridge.BridgeConfigError):
        codex_bridge.validate(_cfg(auto_approve=True))


def test_auto_approve_missing_fails_closed():
    cfg = _cfg()
    del cfg["codex_app_server"]["auto_approve"]
    with pytest.raises(codex_bridge.BridgeConfigError):
        codex_bridge.validate(cfg)


@pytest.mark.parametrize("bad", ["yolo", "", None, "danger", "full-access"])
def test_bad_sandbox_fails_closed(bad):
    with pytest.raises(codex_bridge.BridgeConfigError):
        codex_bridge.validate(_cfg(sandbox=bad))


@pytest.mark.parametrize("bad", ["never", "", None, "always", "auto"])
def test_bad_or_never_approval_policy_fails_closed(bad):
    # 'never' is a real codex policy but it defeats the bridge — must be refused.
    with pytest.raises(codex_bridge.BridgeConfigError):
        codex_bridge.validate(_cfg(approval_policy=bad))
