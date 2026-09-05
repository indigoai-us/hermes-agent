"""Fork patch P14 — approval-voice presentation and external-channel routing.

Flag off ⇒ the stock Block Kit banner path is byte-identical (asserted against
the pre-P14 shape). Flag on ⇒ the ask reads as a person, the raw command is
folded into a threaded ``details:`` reply (never the body), an external channel
routes the ask to the requester's DM, and the confirmation/deny text is spoken
in voice with no internal handle.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules:
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.async_app"] = slack_bolt.async_app
    handler_mod = MagicMock()
    handler_mod.AsyncSocketModeHandler = MagicMock
    sys.modules["slack_bolt.adapter"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode.async_handler"] = handler_mod
    sdk_mod = MagicMock()
    sdk_mod.web = MagicMock()
    sdk_mod.web.async_client = MagicMock()
    sdk_mod.web.async_client.AsyncWebClient = MagicMock
    sys.modules["slack_sdk"] = sdk_mod
    sys.modules["slack_sdk.web"] = sdk_mod.web
    sys.modules["slack_sdk.web.async_client"] = sdk_mod.web.async_client


_ensure_slack_mock()

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.run import _format_exec_approval_fallback  # noqa: E402


_META_CURL = (
    'TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token"); '
    "curl -sS http://169.254.169.254/latest/meta-data/instance-id"
)


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="xoxb-test-token", extra=extra or {})
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._bot_user_id = "U_BOT"
    client = AsyncMock()
    adapter._team_clients = {"T1": client}
    adapter._team_bot_user_ids = {"T1": "U_BOT"}
    adapter._channel_team = {"C1": "T1", "D999": "T1"}
    return adapter, client


def _md(**kw):
    base = {"team_id": "T1"}
    base.update(kw)
    return base


# ===========================================================================
# Flag OFF — stock banner unchanged
# ===========================================================================

class TestFlagOffIsStock:
    @pytest.mark.asyncio
    async def test_stock_banner_and_raw_command_in_body(self):
        adapter, client = _make_adapter()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.1"})

        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=False
        ):
            result = await adapter.send_exec_approval(
                chat_id="C1",
                command="rm -rf /important",
                session_key="s",
                description="dangerous deletion",
                metadata=_md(user_id="U123"),
            )

        assert result.success is True
        # Exactly one send (no details thread reply), stock header + raw command.
        assert client.chat_postMessage.call_count == 1
        body = client.chat_postMessage.call_args.kwargs["blocks"][0]["text"]["text"]
        assert "Command Approval Required" in body
        assert "rm -rf /important" in body


# ===========================================================================
# Flag ON — human voice
# ===========================================================================

class TestFlagOnVoice:
    @pytest.mark.asyncio
    async def test_ask_is_human_and_body_has_no_raw_command(self):
        adapter, client = _make_adapter()
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.1"}, {"ts": "1.2"}]
        )
        # Internal channel (not shared).
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": False}}
        )
        client.users_info = AsyncMock(
            return_value={"user": {"profile": {"display_name": "Jacob"}}}
        )

        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            result = await adapter.send_exec_approval(
                chat_id="C1",
                command=_META_CURL,
                session_key="s",
                metadata=_md(user_id="U123"),
            )

        assert result.success is True
        first = client.chat_postMessage.call_args_list[0].kwargs
        ask = first["blocks"][0]["text"]["text"]
        assert ask == (
            "Jacob, to answer that I need to read my instance details. "
            "OK to go ahead?"
        )
        assert "169.254.169.254" not in ask
        assert "Command Approval Required" not in ask
        # It posted to the (internal) channel, not a DM.
        assert first["channel"] == "C1"
        # Buttons preserved (the gate is intact).
        assert first["blocks"][1]["type"] == "actions"

    @pytest.mark.asyncio
    async def test_raw_command_goes_to_a_details_thread_reply(self):
        adapter, client = _make_adapter()
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.1"}, {"ts": "1.2"}]
        )
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": False}}
        )
        client.users_info = AsyncMock(
            return_value={"user": {"profile": {"display_name": "Jacob"}}}
        )

        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            await adapter.send_exec_approval(
                chat_id="C1", command=_META_CURL, session_key="s",
                metadata=_md(user_id="U123"),
            )

        assert client.chat_postMessage.call_count == 2
        details = client.chat_postMessage.call_args_list[1].kwargs
        assert details["thread_ts"] == "1.1"
        assert details["text"].startswith("details:")
        assert "meta-data" in details["text"]

    @pytest.mark.asyncio
    async def test_external_channel_routes_ask_to_requester_dm(self):
        adapter, client = _make_adapter()
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.1"}, {"ts": "1.2"}]
        )
        # Shared with external members.
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": True}}
        )
        client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D999"}}
        )
        client.users_info = AsyncMock(
            return_value={"user": {"profile": {"display_name": "Jacob"}}}
        )

        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            await adapter.send_exec_approval(
                chat_id="C1", command=_META_CURL, session_key="s",
                metadata=_md(user_id="U123"),
            )

        # Nothing posted in the shared channel — both sends went to the DM.
        posted_channels = {
            c.kwargs["channel"] for c in client.chat_postMessage.call_args_list
        }
        assert posted_channels == {"D999"}
        assert "C1" not in posted_channels

    @pytest.mark.asyncio
    async def test_internal_allowlist_forces_channel_delivery(self):
        adapter, client = _make_adapter(extra={"internal_channels": "C1 C2"})
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.1"}, {"ts": "1.2"}]
        )
        # Even though Slack would flag it shared, the allowlist forces internal.
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": True}}
        )
        client.users_info = AsyncMock(
            return_value={"user": {"profile": {"display_name": "Jacob"}}}
        )

        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            await adapter.send_exec_approval(
                chat_id="C1", command=_META_CURL, session_key="s",
                metadata=_md(user_id="U123"),
            )

        assert client.chat_postMessage.call_args_list[0].kwargs["channel"] == "C1"
        # conversations_open never needed — no DM routing.
        client.conversations_open.assert_not_called()


# ===========================================================================
# Confirmation / deny text via the button handler
# ===========================================================================

class TestButtonOutcomeVoice:
    def test_approve_shows_plain_confirmation_no_handle(self):
        adapter, _ = _make_adapter()
        decision = adapter._approval_decision_text(
            "session", count=1, user_name="me1", voice=True
        )
        assert decision == "Thanks, running it."
        assert "me1" not in decision
        assert "Approved for session" not in decision

    def test_deny_speaks_in_voice(self):
        adapter, _ = _make_adapter()
        decision = adapter._approval_decision_text(
            "deny", count=1, user_name="me1", voice=True
        )
        assert decision == (
            "Skipping that check since I didn't get an OK; "
            "here's what I can say without it."
        )
        assert "me1" not in decision

    def test_timeout_speaks_in_voice(self):
        adapter, _ = _make_adapter()
        decision = adapter._approval_decision_text(
            "once", count=0, user_name="me1", voice=True
        )
        assert "Skipping that check" in decision

    def test_flag_off_keeps_stock_labels(self):
        adapter, _ = _make_adapter()
        assert adapter._approval_decision_text(
            "session", count=1, user_name="me1", voice=False
        ) == "✅ Approved for session by me1"
        assert "expired" in adapter._approval_decision_text(
            "once", count=0, user_name="me1", voice=False
        )


# ===========================================================================
# Gateway text fallback (button-less platforms)
# ===========================================================================

class TestGatewayTextFallback:
    def test_flag_off_is_stock(self):
        with patch("agent.hq_branding.approval_voice_enabled", return_value=False):
            out = _format_exec_approval_fallback(_META_CURL, "dangerous command", "/")
        assert "Dangerous command requires approval" in out
        assert "169.254.169.254" in out  # raw command in the stock body

    def test_flag_on_is_human_with_folded_details(self):
        with patch("agent.hq_branding.approval_voice_enabled", return_value=True):
            out = _format_exec_approval_fallback(_META_CURL, "dangerous command", "/")
        head = out.split("\n", 1)[0]
        assert head == (
            "To answer that I need to read my instance details. OK to go ahead?"
        )
        assert "169.254.169.254" not in head  # not in the body/ask line
        assert "details:" in out  # folded below
        assert "Command Approval Required" not in out
