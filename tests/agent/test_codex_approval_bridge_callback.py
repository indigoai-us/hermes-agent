"""Fork patch P7: the HQ approval-bridge callback that shells the configured
hook adapter with a ``pre_approval_request`` payload.

Covers the exit-code -> approval-choice mapping against a real (fake) adapter
script: exit 0 => "once" (approve), exit 2 => "deny" (reason on stderr),
non-zero => "deny", missing adapter => "deny", timeout => "deny". Everything
that is not a clean exit-0 fails closed to deny.
"""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from agent.codex_runtime import _make_codex_approval_bridge_callback


def _write_adapter(tmp_path, body: str) -> str:
    path = tmp_path / "adapter.sh"
    path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _agent(tmp_path):
    return SimpleNamespace(session_id="sess-1", session_cwd=str(tmp_path))


def _cfg(adapter: str, **extra):
    cas = {
        "approval_bridge": True,
        "auto_approve": False,
        "sandbox": "workspace-write",
        "approval_policy": "untrusted",
        "hook_adapter": adapter,
    }
    cas.update(extra)
    return {"model": {"openai_runtime": "codex_app_server"}, "codex_app_server": cas}


def test_exit0_approves(tmp_path):
    adapter = _write_adapter(tmp_path, "cat >/dev/null; exit 0")
    cb = _make_codex_approval_bridge_callback(_agent(tmp_path), _cfg(adapter))
    assert cb({"type": "exec", "command": "ls", "cwd": str(tmp_path)}) == "once"


def test_exit2_denies(tmp_path):
    adapter = _write_adapter(
        tmp_path, "cat >/dev/null; echo 'blocked by policy' >&2; exit 2")
    cb = _make_codex_approval_bridge_callback(_agent(tmp_path), _cfg(adapter))
    assert cb({"type": "exec", "command": "rm -rf /", "cwd": str(tmp_path)}) == "deny"


def test_nonzero_denies(tmp_path):
    adapter = _write_adapter(tmp_path, "cat >/dev/null; exit 7")
    cb = _make_codex_approval_bridge_callback(_agent(tmp_path), _cfg(adapter))
    assert cb({"type": "exec", "command": "ls", "cwd": str(tmp_path)}) == "deny"


def test_missing_adapter_denies(tmp_path):
    cb = _make_codex_approval_bridge_callback(
        _agent(tmp_path), _cfg(str(tmp_path / "does-not-exist.sh")))
    assert cb({"type": "exec", "command": "ls", "cwd": str(tmp_path)}) == "deny"


def test_empty_adapter_denies(tmp_path):
    cb = _make_codex_approval_bridge_callback(_agent(tmp_path), _cfg(""))
    assert cb({"type": "exec", "command": "ls", "cwd": str(tmp_path)}) == "deny"


def test_timeout_denies(tmp_path):
    adapter = _write_adapter(tmp_path, "cat >/dev/null; sleep 5; exit 0")
    cb = _make_codex_approval_bridge_callback(
        _agent(tmp_path), _cfg(adapter, approval_timeout=0.3))
    assert cb({"type": "exec", "command": "ls", "cwd": str(tmp_path)}) == "deny"


def test_payload_shape_reaches_adapter(tmp_path):
    """The adapter receives a Claude-shaped pre_approval_request payload on
    stdin with the structured approval attached."""
    capture = tmp_path / "seen.json"
    adapter = _write_adapter(tmp_path, f"cat > {capture}; exit 0")
    cb = _make_codex_approval_bridge_callback(_agent(tmp_path), _cfg(adapter))
    req = {"type": "apply_patch", "paths": ["/work/a.py"], "grantRoot": "/work"}
    assert cb(req) == "once"
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["hook_event_name"] == "pre_approval_request"
    assert payload["session_id"] == "sess-1"
    assert payload["approval"] == req


def test_exec_cwd_flows_into_payload(tmp_path):
    capture = tmp_path / "seen.json"
    adapter = _write_adapter(tmp_path, f"cat > {capture}; exit 0")
    cb = _make_codex_approval_bridge_callback(_agent(tmp_path), _cfg(adapter))
    cb({"type": "exec", "command": "ls", "cwd": "/somewhere"})
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["cwd"] == "/somewhere"
    assert payload["approval"]["cwd"] == "/somewhere"
