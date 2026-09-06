"""Fork patch P14.2 — no raw command / shell jargon in a Slack channel.

Reproduces the 2026-09 lilo-social/Stitch approval ask: the brain tried to run
a ``python3 -c 'import os … os.environ …'`` env dump; the approval prompt then
showed "script execution via -e/-c flag" in the ask and the raw command in a
"details:" block in the channel. This locks in: (1) the in-channel ask text
carries none of those surfaces; (2) the raw command is never posted to the
channel — only to the requester's DM (or nowhere).
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


STITCH_CMD = (
    "python3 -c 'import os,json; print(json.dumps({k:v for k,v in "
    'os.environ.items() if "SLACK" in k.upper() or "CHANNEL" in k.upper()}, '
    "sort_keys=True))'"
)
STITCH_DESC = "script execution via -e/-c flag"

# Surfaces that must never appear in ANY text posted to the shared channel.
CHANNEL_FORBIDDEN = [
    "python3 -c",
    "-e/-c",
    "os.environ",
    "details:",
    "script execution",
    "import ",
    "```",
    "SLACK",
]


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


def _texts_for_channel(client, channel):
    """All text/blocks text sent to a given Slack channel id."""
    out = []
    for call in client.chat_postMessage.call_args_list:
        if call.kwargs.get("channel") != channel:
            continue
        if call.kwargs.get("text"):
            out.append(str(call.kwargs["text"]))
        for block in call.kwargs.get("blocks") or []:
            txt = (block.get("text") or {}).get("text")
            if txt:
                out.append(str(txt))
    return "\n".join(out)


@pytest.mark.asyncio
async def test_stitch_env_dump_nothing_leaks_to_internal_channel():
    adapter, client = _make_adapter()
    client.chat_postMessage = AsyncMock(side_effect=[{"ts": "1.1"}, {"ts": "1.2"}])
    client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": {"is_ext_shared": False}}
    )
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D999"}})
    client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "bobby"}}}
    )

    with patch("agent.hq_branding.approval_voice_enabled", return_value=True):
        result = await adapter.send_exec_approval(
            chat_id="C1",
            command=STITCH_CMD,
            session_key="s",
            description=STITCH_DESC,
            metadata=_md(user_id="U123"),
        )

    assert result.success is True
    channel_text = _texts_for_channel(client, "C1")
    # The human ask is present, the leak surfaces are not.
    assert "OK to go ahead?" in channel_text
    for bad in CHANNEL_FORBIDDEN:
        assert bad not in channel_text, f"{bad!r} leaked into channel text"
    # The raw command went to the requester DM, never the channel.
    dm_text = _texts_for_channel(client, "D999")
    assert "os.environ" in dm_text or dm_text == "" or "details:" in dm_text


@pytest.mark.asyncio
async def test_stitch_env_dump_external_channel_posts_nothing_in_channel():
    adapter, client = _make_adapter()
    client.chat_postMessage = AsyncMock(side_effect=[{"ts": "1.1"}, {"ts": "1.2"}])
    client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": {"is_ext_shared": True}}
    )
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D999"}})
    client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "bobby"}}}
    )

    with patch("agent.hq_branding.approval_voice_enabled", return_value=True):
        await adapter.send_exec_approval(
            chat_id="C1",
            command=STITCH_CMD,
            session_key="s",
            description=STITCH_DESC,
            metadata=_md(user_id="U123"),
        )

    posted_channels = {
        c.kwargs["channel"] for c in client.chat_postMessage.call_args_list
    }
    # Nothing at all went to the shared channel.
    assert "C1" not in posted_channels
    assert _texts_for_channel(client, "C1") == ""
