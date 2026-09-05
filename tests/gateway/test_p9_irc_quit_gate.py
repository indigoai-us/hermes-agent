"""Fork patch P9 — the IRC QUIT reason is a gated lifecycle string.

``GatewayConfig.lifecycle_broadcasts_enabled`` (default True = stock upstream
behavior) silences every unprompted, system-generated runtime lifecycle notice
the gateway can push to a platform. P9 gated the runner-driven broadcasts in
``gateway/run.py`` but left the IRC adapter's disconnect QUIT reason — a
human-facing "Hermes Agent shutting down" string sent at the IRC protocol layer
— ungated (incident 2026-09-04; policy
``indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages``). A fleet
agent must not push branded lifecycle boilerplate at a customer IRC channel/
server on disconnect.

Mirrors ``tests/gateway/test_p9_lifecycle_broadcast_gate.py``:
  * behavioral — with the flag OFF the QUIT carries NO human-facing reason (a
    bare ``QUIT``); with the flag ON (default) it sends the stock reason (so the
    gate cannot silently become always-off);
  * a source tripwire — the branded QUIT reason in the IRC adapter stays guarded
    by ``_lifecycle_broadcasts_enabled()`` so it cannot be re-introduced ungated.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_irc_mod = load_plugin_adapter("irc")
IRCAdapter = _irc_mod.IRCAdapter


def _make_adapter(monkeypatch):
    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
        monkeypatch.delenv(key, raising=False)
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "server": "localhost",
            "port": 6667,
            "nickname": "testbot",
            "channel": "#test",
            "use_tls": False,
        },
    )
    return IRCAdapter(cfg)


def _wire_capturing_writer(adapter):
    """Give the adapter a fake writer that records every raw line sent."""
    sent: list[str] = []
    writer = MagicMock()
    writer.is_closing = MagicMock(return_value=False)
    writer.write = MagicMock(side_effect=lambda b: sent.append(b.decode("utf-8")))
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    adapter._writer = writer
    adapter._recv_task = None
    return sent


def _set_lifecycle(monkeypatch, enabled: bool):
    """Drive the real ``_lifecycle_broadcasts_enabled()`` helper by faking the
    gateway config it loads — exercising the actual wiring, not a stub."""
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: SimpleNamespace(lifecycle_broadcasts_enabled=enabled),
    )


# ── behavioral: flag OFF ⇒ no human-facing QUIT reason ───────────────────────


@pytest.mark.asyncio
async def test_quit_reason_suppressed_when_lifecycle_disabled(monkeypatch):
    _set_lifecycle(monkeypatch, False)
    adapter = _make_adapter(monkeypatch)
    sent = _wire_capturing_writer(adapter)

    await adapter.disconnect()

    quit_lines = [ln for ln in sent if ln.startswith("QUIT")]
    assert quit_lines == ["QUIT\r\n"], f"expected a bare QUIT, got {quit_lines!r}"
    assert not any("shutting down" in ln for ln in sent)
    assert not any("Hermes Agent" in ln for ln in sent)


# ── behavioral: flag ON (default) ⇒ stock upstream reason still sent ──────────


@pytest.mark.asyncio
async def test_quit_reason_sent_when_lifecycle_enabled(monkeypatch):
    _set_lifecycle(monkeypatch, True)
    adapter = _make_adapter(monkeypatch)
    sent = _wire_capturing_writer(adapter)

    await adapter.disconnect()

    assert "QUIT :Hermes Agent shutting down\r\n" in sent


@pytest.mark.asyncio
async def test_quit_reason_defaults_to_stock_when_config_unavailable(monkeypatch):
    """The helper fails open (stock upstream behavior) when the gateway config
    cannot be loaded — a broken config must never silence a real user's box."""

    def _boom():
        raise RuntimeError("no config on disk")

    monkeypatch.setattr("gateway.config.load_gateway_config", _boom)
    adapter = _make_adapter(monkeypatch)
    sent = _wire_capturing_writer(adapter)

    await adapter.disconnect()

    assert "QUIT :Hermes Agent shutting down\r\n" in sent


# ── source tripwire: the branded QUIT reason stays gated ─────────────────────


def test_irc_quit_reason_is_gated_in_source():
    """The one branded IRC QUIT reason must sit inside the
    ``_lifecycle_broadcasts_enabled()`` branch; a later edit that emits it
    unconditionally (or adds another branded QUIT reason) fails here."""
    adapter_py = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "platforms"
        / "irc"
        / "adapter.py"
    )
    text = adapter_py.read_text(encoding="utf-8")

    assert "_lifecycle_broadcasts_enabled()" in text, (
        "the IRC adapter must consult the P9 master gate before sending a "
        "human-facing QUIT reason"
    )

    # Every branded QUIT reason line must be reachable only under the gate.
    lines = text.splitlines()
    branded_quit_lines = [
        i for i, ln in enumerate(lines) if 'QUIT :Hermes Agent shutting down' in ln
    ]
    assert branded_quit_lines, "expected the stock branded QUIT reason to exist"
    for idx in branded_quit_lines:
        window = "\n".join(lines[max(0, idx - 6):idx])
        assert "_lifecycle_broadcasts_enabled()" in window, (
            "branded QUIT reason at line "
            f"{idx + 1} is not guarded by the P9 lifecycle gate"
        )
