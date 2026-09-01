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
