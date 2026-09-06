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
    async def test_p143_non_owner_requester_gets_no_details_on_slack(self):
        # P14.3: on an INTERNAL channel the ask + buttons post in-channel, and
        # the command details are NOT sent on Slack at all when the requester is
        # not the agent's owner/admin — not to the channel, not to a thread, not
        # even to the requester's DM. The folded command reaches only the
        # owner's HQ DM (Deacon leak, 2026-09-06). No approval_owner configured
        # ⇒ nobody is an owner ⇒ nothing but the ask.
        adapter, client = _make_adapter()
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.1"}, {"ts": "1.2"}]
        )
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": False}}
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

        # Exactly one send — the ask — to the channel. No details anywhere.
        assert client.chat_postMessage.call_count == 1
        only = client.chat_postMessage.call_args_list[0].kwargs
        assert only["channel"] == "C1"
        for call in client.chat_postMessage.call_args_list:
            blob = str(call.kwargs.get("text", "")) + str(
                call.kwargs.get("blocks", "")
            )
            assert "meta-data" not in blob
            assert "169.254.169.254" not in blob
            assert not str(call.kwargs.get("text", "")).startswith("details:")

    @pytest.mark.asyncio
    async def test_p143_owner_requester_gets_redacted_details_dm_never_channel(self):
        # P14.3: when the requester IS the configured owner, the details reply
        # is delivered to their private DM (a real D… conversation), redacted of
        # runtime/tool internals, secret names and file paths, and NEVER to a
        # channel or a channel thread.
        adapter, client = _make_adapter(extra={"approval_owner": "U123"})
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.1"}, {"ts": "1.2"}]
        )
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": False}}
        )
        client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D999"}}
        )
        client.users_info = AsyncMock(
            return_value={"user": {"profile": {"display_name": "Jacob"}}}
        )

        leak_cmd = (
            "execute_code <<'PY'\nfrom hermes_tools import terminal\n"
            'r=terminal("hq secrets --company indigo exec --only '
            "HQ_OPERATOR_API_COGNITO_USERNAME,HQ_OPERATOR_API_COGNITO_PASSWORD "
            '-- node /tmp/hq_feedback_health_snapshot.mjs",timeout=240)\nPY'
        )
        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            await adapter.send_exec_approval(
                chat_id="C1", command=leak_cmd, session_key="s",
                metadata=_md(user_id="U123"),
            )

        assert client.chat_postMessage.call_count == 2
        ask = client.chat_postMessage.call_args_list[0].kwargs
        details = client.chat_postMessage.call_args_list[1].kwargs
        # Ask went to the channel; details went ONLY to the owner's D… DM.
        assert ask["channel"] == "C1"
        assert details["channel"] == "D999"
        assert str(details["channel"]).startswith("D")
        # Never a channel thread reply.
        assert details.get("thread_ts") is None
        assert details["text"].startswith("details:")
        # The details text is redacted — no forbidden literals leak, even to
        # the owner's DM.
        for forbidden in (
            "hermes_tools",
            "execute_code",
            "terminal(",
            "_PASSWORD",
            "_USERNAME",
            "/tmp/",
        ):
            assert forbidden not in details["text"], forbidden

    @pytest.mark.asyncio
    async def test_p143_channel_shaped_owner_id_never_receives_details(self):
        # P14.3 regression for the Deacon root cause: a channel-shaped id in
        # config/metadata must never resolve to a details target. Even if the
        # requester id matched a (mis-configured) channel-shaped approval_owner,
        # the D… guard drops it — nothing but the ask is sent.
        adapter, client = _make_adapter(extra={"approval_owner": "C1"})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.1"})
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": False}}
        )
        # conversations_open echoes a channel id back (simulating the P14.2
        # _ensure_dm_conversation passthrough that caused the leak).
        client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "C1"}}
        )

        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            await adapter.send_exec_approval(
                chat_id="C1", command=_META_CURL, session_key="s",
                metadata=_md(user_id="C1"),  # channel-shaped "requester"
            )

        # Only the ask — the channel-shaped target never gets a details reply.
        assert client.chat_postMessage.call_count == 1
        only = client.chat_postMessage.call_args_list[0].kwargs
        assert not str(only.get("text", "")).startswith("details:")
        assert "meta-data" not in str(only.get("blocks", ""))

    @pytest.mark.asyncio
    async def test_p143_no_slack_bound_approval_message_carries_forbidden_literal(self):
        # P14.3 tripwire: across every Slack-bound message in the approval flow
        # (owner DM path included), no runtime/tool internal, secret name, or
        # file path ever leaves. Uses the exact Deacon leak command.
        from agent import hq_branding

        adapter, client = _make_adapter(extra={"approval_owner": "U123"})
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.1"}, {"ts": "1.2"}]
        )
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": False}}
        )
        client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D999"}}
        )
        client.users_info = AsyncMock(
            return_value={"user": {"profile": {"display_name": "Jacob"}}}
        )

        leak_cmd = (
            "execute_code <<'PY'\nfrom hermes_tools import terminal\n"
            'r=terminal("hq secrets --only FOO_TOKEN,BAR_SECRET -- '
            'node /tmp/x.mjs",timeout=240)\nPY'
        )
        # Also try to smuggle a secret name via the description path.
        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            await adapter.send_exec_approval(
                chat_id="C1", command=leak_cmd, session_key="s",
                description="dump AWS_SECRET_ACCESS_KEY from /tmp/env",
                metadata=_md(user_id="U123"),
            )

        for call in client.chat_postMessage.call_args_list:
            blob = (
                str(call.kwargs.get("text", ""))
                + str(call.kwargs.get("blocks", ""))
            )
            assert hq_branding.contains_forbidden_approval_literal(blob) is None, blob

    @pytest.mark.asyncio
    async def test_raw_command_withheld_when_no_private_target(self):
        # P14.2: internal channel + unknown requester and no configured owner ⇒
        # the command goes NOWHERE (never the channel). Only the ask posts.
        adapter, client = _make_adapter()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.1"})
        client.conversations_info = AsyncMock(
            return_value={"ok": True, "channel": {"is_ext_shared": False}}
        )
        client.conversations_open = AsyncMock(
            return_value={"channel": {"id": "D999"}}
        )

        with patch(
            "agent.hq_branding.approval_voice_enabled", return_value=True
        ):
            await adapter.send_exec_approval(
                chat_id="C1", command=_META_CURL, session_key="s",
                metadata=_md(),  # no user_id, no owner
            )

        # Exactly one send — the ask — and it went to the channel; no details.
        assert client.chat_postMessage.call_count == 1
        only = client.chat_postMessage.call_args_list[0].kwargs
        assert only["channel"] == "C1"
        assert "meta-data" not in only["blocks"][0]["text"]["text"]

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

        # The ASK is forced to the internal channel (not DM'd).
        assert client.chat_postMessage.call_args_list[0].kwargs["channel"] == "C1"
        # P14.2: the raw command still never posts to the channel — it goes to
        # the requester DM. So no C1 message carries the command.
        for call in client.chat_postMessage.call_args_list:
            if call.kwargs.get("channel") == "C1":
                blob = str(call.kwargs.get("text", "")) + str(
                    call.kwargs.get("blocks", "")
                )
                assert "meta-data" not in blob
                assert "169.254.169.254" not in blob


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

    def test_flag_on_is_human_with_no_scaffold_or_command(self):
        # P14.1: the buttonless fallback is a plain human ask + a scaffold-free
        # reply hint. No /approve|/deny scaffold, no raw command, no banner —
        # the exact leak that reached a user DM on odin (2026-09-06).
        with patch("agent.hq_branding.approval_voice_enabled", return_value=True):
            out = _format_exec_approval_fallback(_META_CURL, "dangerous command", "/")
        head = out.split("\n", 1)[0]
        assert head == (
            "To answer that I need to read my instance details. OK to go ahead?"
        )
        # The whole message — not just the head — is free of the leak surfaces.
        assert "169.254.169.254" not in out          # raw command never in body
        assert "details:" not in out                  # no inline fold on a buttonless surface
        assert "/approve" not in out                  # no approval scaffold
        assert "/deny" not in out
        assert "approve session" not in out
        assert "approve always" not in out
        assert "Command Approval Required" not in out
        assert "Dangerous command requires approval" not in out
        assert "⚠" not in out
        # It still tells a person how to answer, in plain words the gateway's
        # has_blocking_approval intercept resolves.
        assert "yes" in out.lower()
        assert "no" in out.lower()

    def test_flag_on_tripwire_no_scaffold_literals_across_variants(self):
        forbidden = ("/approve", "/deny", "approve session", "approve always",
                     "Command Approval Required", "⚠", "details:")
        with patch("agent.hq_branding.approval_voice_enabled", return_value=True):
            for allow_session in (True, False):
                for allow_permanent in (True, False):
                    for smart_denied in (True, False):
                        out = _format_exec_approval_fallback(
                            _META_CURL, "dangerous command", "/",
                            allow_permanent=allow_permanent,
                            allow_session=allow_session,
                            smart_denied=smart_denied,
                        )
                        for bad in forbidden:
                            assert bad not in out, (
                                f"{bad!r} leaked with session={allow_session} "
                                f"permanent={allow_permanent} smart_denied={smart_denied}"
                            )
                        assert "169.254.169.254" not in out
