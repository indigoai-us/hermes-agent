"""Opt-in codex context bridge (agent.codex_flags).

The codex app-server path historically dropped every Hermes-side context
seam: the composed system prompt was never sent (codex ran on its own base
instructions + cwd AGENTS.md only) and the per-turn plugin/memory context
from ``pre_llm_call`` was discarded (the thread got the raw user text).

Two default-off flags forward that context:

* ``agent.codex_forward_system_prompt`` → thread/start carries the Hermes
  system prompt as ``developerInstructions`` (protocol v2; codex keeps its
  own base instructions).
* ``agent.codex_forward_plugin_context`` → the turn input is composed via
  ``compose_user_api_content`` exactly like the standard loop's API copy.

These tests lock in: default-off (raw behavior preserved), flag-on
forwarding, and the graceful retry when an older codex rejects
``developerInstructions``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_runtime import run_codex_app_server_turn
from agent.transports.codex_app_server_session import CodexAppServerSession


def _make_turn():
    return SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="turn-1",
        projected_messages=[{"role": "assistant", "content": "OK"}],
        tool_iterations=0,
        final_text="OK",
        should_retire=False,
    )


def _make_agent():
    agent = MagicMock()
    # Pre-seeded session: the spawn block (and its ctor) is skipped, so
    # these tests exercise the per-turn input path in isolation.
    agent._codex_session = MagicMock()
    agent._codex_session.run_turn.return_value = _make_turn()
    agent.api_mode = "codex_app_server"
    agent.compression_checkpoint_required = False
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._session_db = None
    agent._session_db_created = False
    agent.session_id = "sess-bridge"
    agent._interrupt_requested = False
    return agent


def _run(agent, **kwargs):
    base = dict(
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-1",
    )
    base.update(kwargs)
    return run_codex_app_server_turn(agent, **base)


# ── turn-input composition ──────────────────────────────────────────────────

def test_plugin_context_dropped_by_default(monkeypatch):
    agent = _make_agent()
    _run(agent, plugin_user_context="PLUGIN CTX", ext_prefetch_cache="MEM")
    sent = agent._codex_session.run_turn.call_args.kwargs["user_input"]
    assert sent == "hello"  # historical behavior: raw user text only


def test_plugin_context_forwarded_when_flag_on(monkeypatch):
    import agent.codex_flags as codex_flags

    monkeypatch.setattr(
        codex_flags, "codex_forward_plugin_context", lambda config=None: True
    )
    agent = _make_agent()
    _run(agent, plugin_user_context="PLUGIN CTX", ext_prefetch_cache="MEM")
    sent = agent._codex_session.run_turn.call_args.kwargs["user_input"]
    assert "hello" in sent
    assert "PLUGIN CTX" in sent
    assert "MEM" in sent


def test_composition_failure_falls_back_to_raw_message(monkeypatch):
    import agent.codex_flags as codex_flags

    monkeypatch.setattr(
        codex_flags,
        "codex_forward_plugin_context",
        lambda config=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    agent = _make_agent()
    _run(agent, plugin_user_context="PLUGIN CTX")
    sent = agent._codex_session.run_turn.call_args.kwargs["user_input"]
    assert sent == "hello"


# ── thread/start developerInstructions ──────────────────────────────────────

class _FakeClient:
    """Minimal thread/start server; optionally rejects developerInstructions."""

    def __init__(self, *args, reject_developer_instructions=False, **kwargs):
        self.reject = reject_developer_instructions
        self.requests = []

    def initialize(self, **kwargs):
        return {}

    def request(self, method, params, timeout=None):
        self.requests.append((method, dict(params)))
        if method == "thread/start":
            if self.reject and "developerInstructions" in params:
                from agent.transports.codex_app_server import (
                    CodexAppServerError,
                )

                raise CodexAppServerError(
                    -32602, "unknown field developerInstructions"
                )
            return {"thread": {"id": "thread-9"}}
        return {}


def _session(dev_instructions, reject=False):
    client = _FakeClient(reject_developer_instructions=reject)
    session = CodexAppServerSession(
        cwd="/tmp",
        client_factory=lambda *a, **k: client,
        developer_instructions=dev_instructions,
    )
    session.ensure_started()
    return client


def test_thread_start_omits_developer_instructions_when_unset():
    client = _session(None)
    method, params = client.requests[0]
    assert method == "thread/start"
    assert "developerInstructions" not in params


def test_thread_start_carries_developer_instructions():
    client = _session("HERMES SYSTEM PROMPT")
    method, params = client.requests[0]
    assert method == "thread/start"
    assert params["developerInstructions"] == "HERMES SYSTEM PROMPT"


def test_thread_start_retries_without_developer_instructions_on_reject():
    client = _session("HERMES SYSTEM PROMPT", reject=True)
    starts = [r for r in client.requests if r[0] == "thread/start"]
    assert len(starts) == 2
    assert "developerInstructions" in starts[0][1]
    assert "developerInstructions" not in starts[1][1]
