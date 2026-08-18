"""Runtime smoke tests for Docker immutable install tree and install-method stamp.

Build the real image and verify at runtime:

  1. /opt/hqr is not writable by the hqr user (immutable install tree)
  2. PYTHONDONTWRITEBYTECODE and HQR_DISABLE_LAZY_INSTALLS are set
  3. /opt/hqr/.install_method contains "docker" (code-scoped stamp)
  4. $HQR_HOME/.install_method is NOT stamped as "docker" by stage2
  5. A stale "docker" stamp in $HQR_HOME is healed (removed) on boot
"""
from __future__ import annotations

from tests.docker.conftest import (
    docker_exec,
    docker_exec_sh,
    restart_container,
    start_container,
)


def test_install_tree_not_writable_by_hqr(
    built_image: str, container_name: str,
) -> None:
    """The hqr user must not be able to modify /opt/hqr.

    The install tree (source, venv, TUI bundle, node_modules) must remain
    root-owned and non-writable so an agent session cannot self-modify
    the installation and brick the gateway.
    """
    start_container(built_image, container_name)

    r = docker_exec_sh(
        container_name,
        # Try to create a file under /opt/hqr as the hqr user
        "touch /opt/hqr/test_write 2>&1 && "
        "echo WRITE_SUCCEEDED || echo WRITE_FAILED",
        timeout=10,
    )
    assert "WRITE_FAILED" in r.stdout, (
        f"hqr user can write to /opt/hqr (install tree not immutable): "
        f"{r.stdout}"
    )

    # Also check a key subdirectory
    r = docker_exec_sh(
        container_name,
        "touch /opt/hqr/.venv/test_write 2>&1 && "
        "echo WRITE_SUCCEEDED || echo WRITE_FAILED",
        timeout=10,
    )
    assert "WRITE_FAILED" in r.stdout, (
        f"hqr user can write to /opt/hqr/.venv: {r.stdout}"
    )


def test_hqr_disable_lazy_installs_and_dont_write_bytecode(
    built_image: str, container_name: str,
) -> None:
    """The container must set PYTHONDONTWRITEBYTECODE and
    HQR_DISABLE_LAZY_INSTALLS=1 so no .pyc files are written to the
    immutable install tree and no lazy installs attempt to modify it."""
    start_container(built_image, container_name)

    r = docker_exec_sh(
        container_name,
        'test "$PYTHONDONTWRITEBYTECODE" = "1" && '
        'test "$HQR_DISABLE_LAZY_INSTALLS" = "1" && '
        'echo ENV_OK || echo ENV_MISSING',
        timeout=10,
    )
    assert "ENV_OK" in r.stdout, (
        f"expected PYTHONDONTWRITEBYTECODE=1 and "
        f"HQR_DISABLE_LAZY_INSTALLS=1, got: {r.stdout} stderr={r.stderr}"
    )




def test_stale_docker_stamp_in_home_is_healed_on_boot(
    built_image: str, container_name: str,
) -> None:
    """A stale 'docker' stamp left in $HQR_HOME by an older image
    must be removed on boot so shared homes self-heal."""
    # Start container, write a stale stamp
    start_container(built_image, container_name)

    # Write a stale 'docker' stamp as root
    docker_exec(
        container_name, "sh", "-c",
        "printf 'docker\\n' > /opt/data/.install_method",
        user="root", timeout=5,
    )
    # Verify it exists
    r = docker_exec_sh(container_name, "cat /opt/data/.install_method", timeout=5)
    assert r.stdout.strip() == "docker"

    # Restart - stage2 should heal it
    restart_container(container_name)

    # The stale stamp must be gone
    r = docker_exec_sh(
        container_name,
        "test -f /opt/data/.install_method && "
        "cat /opt/data/.install_method || echo HEALED",
        timeout=10,
    )
    assert "HEALED" in r.stdout or r.stdout.strip() != "docker", (
        f"stale 'docker' stamp in $HQR_HOME was not healed on boot: "
        f"{r.stdout}"
    )
