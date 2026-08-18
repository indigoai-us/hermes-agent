"""Tests for hqr_cli.gui_uninstall — GUI-only uninstall + install discovery.

Covers the cross-platform artifact discovery, the agent/GUI detection the
desktop UI gates options on, and that ``uninstall_gui`` removes only GUI
artifacts (built renderer/release/node_modules, packaged bundle, Electron
userData) while leaving the Python agent + config/sessions/.env intact.
"""

import sys
from pathlib import Path

import pytest

import hqr_cli.gui_uninstall as gu


def _make_agent(hqr_home: Path) -> Path:
    """Create a fake agent install: source package + venv."""
    agent_root = hqr_home / "hqr-agent"
    (agent_root / "hqr_cli").mkdir(parents=True)
    (agent_root / "hqr_cli" / "__init__.py").write_text("")
    (agent_root / "venv" / "bin").mkdir(parents=True)
    return agent_root


def _make_gui_build(hqr_home: Path) -> None:
    """Create the source-built GUI artifacts a `hqr desktop` run produces."""
    desktop = hqr_home / "hqr-agent" / "apps" / "desktop"
    (desktop / "dist").mkdir(parents=True)
    (desktop / "dist" / "index.html").write_text("<html>")
    (desktop / "release" / "linux-unpacked").mkdir(parents=True)
    (desktop / "node_modules").mkdir(parents=True)
    (hqr_home / "hqr-agent" / "node_modules").mkdir(parents=True)
    (hqr_home / "desktop-build-stamp.json").write_text("{}")


def _make_user_data(hqr_home: Path) -> None:
    (hqr_home / "config.yaml").write_text("x: 1\n")
    (hqr_home / ".env").write_text("KEY=secret\n")
    (hqr_home / "sessions").mkdir()










def test_gui_install_summary_shape(tmp_path, monkeypatch):
    hqr_home = tmp_path / ".hqr"
    _make_agent(hqr_home)
    _make_gui_build(hqr_home)
    monkeypatch.setattr(gu, "packaged_gui_app_paths", lambda: [])
    monkeypatch.setattr(gu, "desktop_userdata_dir", lambda: tmp_path / "none")

    summary = gu.gui_install_summary(hqr_home)
    # JSON-serializable primitives the desktop UI gates on.
    assert summary["agent_installed"] is True
    assert summary["gui_installed"] is True
    assert isinstance(summary["source_built_artifacts"], list)
    assert all(isinstance(p, str) for p in summary["source_built_artifacts"])
    assert summary["hqr_home"] == str(hqr_home)
    assert summary["platform"] == sys.platform






def test_linux_discovery_includes_launcher_entry(tmp_path, monkeypatch):
    """The launcher entry that `hqr desktop` installs is removable."""
    monkeypatch.setattr(gu.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    from hqr_cli import linux_desktop_entry as lde

    assert lde.desktop_entry_path() in gu.packaged_gui_app_paths()


def test_uninstall_removes_launcher_entry_and_refreshes_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(gu.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    from hqr_cli import linux_desktop_entry as lde

    entry = lde.desktop_entry_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("x", encoding="utf-8")

    refreshed: list[Path] = []
    monkeypatch.setattr(
        lde, "refresh_desktop_databases", lambda d: refreshed.append(d) or ["kbuildsycoca6"]
    )

    hqr_home = tmp_path / ".hqr"
    _make_agent(hqr_home)
    icon = lde.icon_path(hqr_home / "hqr-agent")
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(b"\x89PNG")
    monkeypatch.setattr(gu, "desktop_userdata_dir", lambda: tmp_path / "none")

    removed = gu.uninstall_gui(hqr_home)

    assert entry in removed and not entry.exists()
    assert refreshed == [entry.parent]
    # The icon lives in the checkout. A GUI uninstall must not delete it.
    assert lde.icon_path(hqr_home / "hqr-agent").exists()
    # The agent itself survives a GUI uninstall.
    assert (hqr_home / "hqr-agent" / "hqr_cli").is_dir()


def test_uninstall_skips_cache_refresh_when_no_launcher_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(gu.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    from hqr_cli import linux_desktop_entry as lde

    refreshed: list[Path] = []
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda d: refreshed.append(d) or [])
    monkeypatch.setattr(gu, "desktop_userdata_dir", lambda: tmp_path / "none")

    gu.uninstall_gui(tmp_path / ".hqr")

    assert refreshed == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_remove_path_handles_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert gu._remove_path(link) is True
    assert not link.exists()
    # The symlink is gone but its target is untouched.
    assert target.exists()


class _Args:
    """Minimal argparse-Namespace stand-in for run_uninstall."""

    def __init__(self, *, yes=False, full=False, gui=False, gui_summary=False):
        self.yes = yes
        self.full = full
        self.gui = gui
        self.gui_summary = gui_summary








def test_uninstall_args_namespace_mode_mapping():
    """_UninstallArgs maps mode → the gui/full flags run_uninstall reads."""
    import hqr_cli.uninstall as uninstall

    gui = uninstall._UninstallArgs(mode="gui")
    assert gui.gui is True and gui.full is False and gui.yes is True

    lite = uninstall._UninstallArgs(mode="lite")
    assert lite.gui is False and lite.full is False and lite.yes is True

    full = uninstall._UninstallArgs(mode="full")
    assert full.gui is False and full.full is True and full.yes is True

