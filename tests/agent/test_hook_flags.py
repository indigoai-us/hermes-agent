"""Dispatcher-level fail-closed for ``pre_tool_call`` (agent/hook_flags.py).

Per-hook ``fail_closed`` covers a FAILING HOOK; these cover the layer above —
the dispatcher itself throwing (import failure, hook-plumbing bug). Default
keeps today's fail-open; ``agent.hooks_dispatcher_fail_closed: true`` turns a
dispatcher failure into a block whose message names the flag.
"""

from unittest.mock import patch

from agent.hook_flags import (
    dispatcher_fail_closed_message,
    hooks_dispatcher_fail_closed,
)
from model_tools import handle_function_call


class TestHookFlagParsing:
    def test_defaults_off(self):
        assert hooks_dispatcher_fail_closed({}) is False
        assert hooks_dispatcher_fail_closed({"agent": {}}) is False

    def test_truthy_agent_config_enables(self):
        assert (
            hooks_dispatcher_fail_closed(
                {"agent": {"hooks_dispatcher_fail_closed": True}}
            )
            is True
        )
        assert (
            hooks_dispatcher_fail_closed(
                {"agent": {"hooks_dispatcher_fail_closed": "true"}}
            )
            is True
        )

    def test_malformed_config_is_off(self):
        assert hooks_dispatcher_fail_closed({"agent": "nope"}) is False
        assert (
            hooks_dispatcher_fail_closed(
                {"agent": {"hooks_dispatcher_fail_closed": "garbage"}}
            )
            is False
        )

    def test_message_names_the_flag_and_the_error(self):
        message = dispatcher_fail_closed_message(RuntimeError("boom"))
        assert "hooks_dispatcher_fail_closed" in message
        assert "boom" in message


class TestModelToolsDispatcherFailClosed:
    def test_dispatcher_exception_blocks_when_flag_on(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}') as dispatch,
            patch(
                "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
                side_effect=RuntimeError("plumbing broke"),
            ),
            patch("agent.hook_flags.hooks_dispatcher_fail_closed", return_value=True),
        ):
            result = handle_function_call("web_search", {"q": "x"}, task_id="t1")

        # The failure became the tool result; the tool itself never ran.
        assert "hooks_dispatcher_fail_closed" in result
        assert "plumbing broke" in result
        dispatch.assert_not_called()

    def test_dispatcher_exception_stays_fail_open_by_default(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}') as dispatch,
            patch(
                "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
                side_effect=RuntimeError("plumbing broke"),
            ),
            patch("agent.hook_flags.hooks_dispatcher_fail_closed", return_value=False),
        ):
            result = handle_function_call("web_search", {"q": "x"}, task_id="t1")

        # Unchanged legacy behavior: the tool runs as if no hook were wired.
        assert result == '{"ok":true}'
        dispatch.assert_called_once()


class TestShellReentrantFlagParsing:
    def test_defaults_off(self):
        from agent.hook_flags import hooks_shell_reentrant

        assert hooks_shell_reentrant({}) is False
        assert hooks_shell_reentrant({"agent": {}}) is False

    def test_truthy_agent_config_enables(self):
        from agent.hook_flags import hooks_shell_reentrant

        assert hooks_shell_reentrant({"agent": {"hooks_shell_reentrant": True}}) is True
        assert hooks_shell_reentrant({"agent": {"hooks_shell_reentrant": "true"}}) is True

    def test_malformed_config_is_off(self):
        from agent.hook_flags import hooks_shell_reentrant

        assert hooks_shell_reentrant({"agent": "nope"}) is False
        assert hooks_shell_reentrant({"agent": {"hooks_shell_reentrant": "garbage"}}) is False


class TestShellHookReentrancy:
    """P6: two in-flight fires of one shell-hook callback must not refuse the second.

    The dispatcher single-flights every callback under the timeout path: while
    one fire is running, a second fire of the same callback is skipped, and a
    skipped ``pre_tool_call`` fails closed (the tool is refused). Shell hooks
    are one subprocess per fire with their own timeout + process-tree kill, so
    the agent's parallel tool batches were being refused for no reason (live
    2026-09-02: 10 of 12 pre_tool_call fires). With the flag on, a callback
    marked ``hermes_reentrant`` runs concurrently; with it off, stock behavior.
    """

    @staticmethod
    def _slow_reentrant_callback(release):
        def _cb(**_kwargs):
            release.wait(timeout=10.0)
            return None

        _cb.__name__ = "shell_hook[pre_tool_call:/x/adapter.sh pre_tool_call]"
        _cb.hermes_reentrant = True
        return _cb

    @staticmethod
    def _two_concurrent_fires(mgr, monkeypatch, release):
        import threading

        import hermes_cli.plugins as plugins_mod
        from hermes_cli.plugins import resolve_pre_tool_block

        monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
        results = {}

        def _fire(tag):
            results[tag] = resolve_pre_tool_block("read_file", {"path": tag})

        t1 = threading.Thread(target=_fire, args=("a",))
        t2 = threading.Thread(target=_fire, args=("b",))
        t1.start()
        # Let the first fire register as running before the second arrives.
        import time

        time.sleep(0.15)
        t2.start()
        time.sleep(0.15)
        release.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        return results

    def test_flag_on_second_concurrent_fire_is_not_refused(self, monkeypatch):
        import threading

        from hermes_cli.plugins import PluginManager

        monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 2.0)
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"agent": {"hooks_shell_reentrant": True}},
        )
        release = threading.Event()
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [self._slow_reentrant_callback(release)]

        results = self._two_concurrent_fires(mgr, monkeypatch, release)

        assert results["a"] is None
        assert results["b"] is None, "second concurrent fire was refused"
        assert not mgr._hook_running_callbacks, "running-callback bookkeeping leaked"

    def test_flag_off_keeps_stock_single_flight(self, monkeypatch):
        import threading

        from hermes_cli.plugins import (
            _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE,
            PluginManager,
        )

        monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 2.0)
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"agent": {}})
        release = threading.Event()
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [self._slow_reentrant_callback(release)]

        results = self._two_concurrent_fires(mgr, monkeypatch, release)

        assert results["a"] is None
        assert results["b"] == _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE

    def test_flag_on_does_not_unblock_non_reentrant_callbacks(self, monkeypatch):
        import threading

        from hermes_cli.plugins import (
            _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE,
            PluginManager,
        )

        monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 2.0)
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"agent": {"hooks_shell_reentrant": True}},
        )
        release = threading.Event()

        def plain_python_policy(**_kwargs):
            release.wait(timeout=10.0)
            return None

        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [plain_python_policy]

        results = self._two_concurrent_fires(mgr, monkeypatch, release)

        assert results["a"] is None
        assert results["b"] == _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE

    def test_shell_hook_callbacks_carry_the_marker(self):
        from agent.shell_hooks import ShellHookSpec, _make_callback

        spec = ShellHookSpec(event="pre_tool_call", command="/x/adapter.sh pre_tool_call")
        cb = _make_callback(spec)
        assert getattr(cb, "hermes_reentrant", False) is True
