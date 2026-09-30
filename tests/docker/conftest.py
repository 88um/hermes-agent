"""Shared fixtures for docker-image integration tests.

Tests in this directory build the image with the current ``Dockerfile``
and exercise it via ``docker run``. They skip when Docker is unavailable
(e.g. on developer laptops without a daemon).

Override the image with ``HERMES_TEST_IMAGE`` env var to point at a pre-built
image (faster local iteration); otherwise the ``built_image`` fixture builds
the repo's Dockerfile once per session.

"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

IMAGE_TAG = os.environ.get("HERMES_TEST_IMAGE", "hermes-agent-harness:latest")

# ---------------------------------------------------------------------------
# Orphan reaping
# ---------------------------------------------------------------------------
#
# Every container these tests create runs ``sleep infinity``, so an orphan
# survives forever and holds its full memory footprint. Fixture teardown
# covers the normal path, but pytest never runs teardown when the process
# dies hard -- double Ctrl-C, SIGKILL, a CI timeout, or the OOM killer. We
# found six such containers still alive 35 hours after the run that made
# them, on a laptop that was by then swapping itself to death.
#
# Two nets catch that:
#   1. Every name created here is registered below and removed in
#      ``pytest_sessionfinish``. That also covers a fixture that raises
#      *before* its ``yield`` -- pytest skips teardown entirely in that case.
#   2. Before the first test runs, anything matching our naming scheme that
#      is older than ``ORPHAN_TTL_S`` is reaped. The age gate is what makes
#      this safe alongside a concurrently running suite: a live run's
#      containers are minutes old, never hours.

CONTAINER_PREFIXES = ("hermes-test-", "hermes-restart-")
VOLUME_PREFIX = "hermes-restart-vol-"

#: Objects older than this are treated as orphans from a crashed run. Set
#: well above any real test duration so a parallel suite is never touched.
ORPHAN_TTL_S = float(os.environ.get("HERMES_TEST_ORPHAN_TTL_S", 2 * 3600))

_SESSION_CONTAINERS: set[str] = set()
_SESSION_VOLUMES: set[str] = set()

# Docker stamps RFC3339 with nanosecond precision ("...789012345Z"), which
# datetime.fromisoformat rejects -- capture the parts we can parse.
_RFC3339_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?")


def _docker_cli(
    *args: str, timeout: int = 30,
) -> subprocess.CompletedProcess[str] | None:
    """Run ``docker <args>``, returning None instead of raising.

    Reaping is best-effort housekeeping: a docker hiccup here must never
    fail the suite.
    """
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def register_container(name: str) -> str:
    """Track ``name`` for the end-of-session sweep and return it."""
    _SESSION_CONTAINERS.add(name)
    return name


def register_volume(name: str) -> str:
    """Track a named volume for the end-of-session sweep and return it."""
    _SESSION_VOLUMES.add(name)
    return name


def force_remove_container(name: str) -> None:
    """``docker rm -f`` ``name`` and drop it from the session registry."""
    _docker_cli("rm", "-f", name, timeout=15)
    _SESSION_CONTAINERS.discard(name)


def force_remove_volume(name: str) -> None:
    """``docker volume rm -f`` ``name`` and drop it from the registry.

    Fails harmlessly while a container still references the volume.
    """
    _docker_cli("volume", "rm", "-f", name, timeout=15)
    _SESSION_VOLUMES.discard(name)


def _age_s(name: str, *inspect_args: str) -> float | None:
    """Seconds since ``name`` was created, or None if it can't be read."""
    r = _docker_cli(*inspect_args, timeout=10)
    if r is None or r.returncode != 0:
        return None
    m = _RFC3339_RE.match(r.stdout.strip())
    if m is None:
        return None
    try:
        created = datetime.fromisoformat(
            f"{m.group(1)}{(m.group(2) or '')[:7]}+00:00",
        )
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - created).total_seconds()


def _list_by_prefix(prefix: str, *list_args: str) -> list[str]:
    """Names reported by ``docker <list_args>`` that start with ``prefix``.

    The prefix is re-checked in Python so a daemon that treats the ``^``
    anchor loosely can't widen what we delete.
    """
    r = _docker_cli(*list_args, timeout=15)
    if r is None or r.returncode != 0:
        return []
    return [n for n in r.stdout.split() if n.startswith(prefix)]


def reap_orphans(min_age_s: float) -> int:
    """Remove leftover test containers/volumes older than ``min_age_s``.

    Objects whose age can't be determined are left alone -- deleting on
    "don't know" is exactly the case that could hit a parallel run.
    Returns the number of objects removed.
    """
    removed = 0
    for prefix in CONTAINER_PREFIXES:
        for name in _list_by_prefix(
            prefix, "ps", "-a", "--filter", f"name=^{prefix}",
            "--format", "{{.Names}}",
        ):
            age = _age_s(name, "inspect", "-f", "{{.Created}}", name)
            if age is None or age < min_age_s:
                continue
            force_remove_container(name)
            removed += 1
    for name in _list_by_prefix(
        VOLUME_PREFIX, "volume", "ls", "--filter", f"name=^{VOLUME_PREFIX}",
        "--format", "{{.Name}}",
    ):
        age = _age_s(
            name, "volume", "inspect", "-f", "{{.CreatedAt}}", name,
        )
        if age is None or age < min_age_s:
            continue
        force_remove_volume(name)
        removed += 1
    return removed


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """Remove every container/volume this session created.

    Fixture teardown normally does this; the registry catches what teardown
    can't -- most importantly a fixture that failed before its ``yield``.
    """
    for name in list(_SESSION_CONTAINERS):
        force_remove_container(name)
    for name in list(_SESSION_VOLUMES):
        force_remove_volume(name)



def _docker_available() -> bool:
    """Return True iff a docker CLI is on PATH and the daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def pytest_collection_modifyitems(config, items):  # noqa: D401 - pytest hook
    """Apply docker-suite policy: timeout bump + skip on missing docker.

    Also reaps orphans from earlier crashed runs before the first test
    starts -- this is the only net that catches a previous session killed
    by SIGKILL or the OOM killer, which runs no teardown at all.
    """
    docker_ok = _docker_available()
    skip_docker = pytest.mark.skip(
        reason="Docker not available or daemon not running",
    )
    has_docker_items = False
    for item in items:
        if "tests/docker/" not in str(item.fspath).replace(os.sep, "/"):
            continue
        has_docker_items = True
        if not docker_ok:
            item.add_marker(skip_docker)
    if docker_ok and has_docker_items:
        reap_orphans(ORPHAN_TTL_S)


@pytest.fixture(scope="session")
def built_image() -> str:
    """Build the image once per test session.

    Override with ``HERMES_TEST_IMAGE`` env var to point at a pre-built
    image (faster local iteration).
    """
    if os.environ.get("HERMES_TEST_IMAGE"):
        return IMAGE_TAG
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."),
    )
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, repo_root],
        capture_output=True, text=True, timeout=1200,
    )
    assert result.returncode == 0, (
        f"docker build failed:\n{result.stderr[-2000:]}"
    )
    return IMAGE_TAG


@pytest.fixture
def container_name(request) -> Iterator[str]:
    """Generate a unique container name and ensure cleanup on test exit."""
    safe = request.node.name.replace("[", "_").replace("]", "_")
    name = register_container(f"hermes-test-{safe}")
    yield name
    force_remove_container(name)


# ---------------------------------------------------------------------------
# docker_exec — default to the unprivileged hermes user
# ---------------------------------------------------------------------------
#
# Background: every Hermes runtime path inside the container drops to UID
# 10000 (the ``hermes`` user) via ``s6-setuidgid hermes``. ``docker exec``
# without ``-u`` runs as root, which is **not** representative of how
# production code executes. PR #30136 review caught a real regression
# this way — ``Path('/proc/1/exe').resolve()`` works as root and silently
# fails (PermissionError swallowed) for hermes, so a test that ran as root
# couldn't catch a feature that was inert for the actual runtime user.
#
# Tests in this directory MUST exercise the realistic user context. The
# helpers below run every probe under ``-u hermes`` unless a specific
# test explicitly opts into ``user="root"`` (rare — e.g. inspecting
# /proc/1/exe itself, chowning a volume).
# ---------------------------------------------------------------------------


def docker_exec(
    container: str,
    *args: str,
    user: str = "hermes",
    timeout: int = 30,
    extra_docker_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a command inside ``container`` as ``user`` (default: hermes).

    Returns the CompletedProcess with text=True, capture_output=True.

    Pass ``user="root"`` only when the test specifically needs root
    capabilities (e.g. reading /proc/1/exe, manipulating ownership).
    Most tests should use the default.
    """
    cmd = ["docker", "exec", "-u", user, *extra_docker_args, container, *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )


def docker_exec_sh(
    container: str,
    command: str,
    *,
    user: str = "hermes",
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run ``sh -c <command>`` inside the container as ``user``."""
    return docker_exec(
        container, "sh", "-c", command, user=user, timeout=timeout,
    )


def wait_for_container_ready(
    container: str,
    *,
    deadline_s: float = 30.0,
    interval_s: float = 0.25,
) -> None:
    """Poll until the container has finished s6 cont-init (stage2 + reconcile).

    The readiness signal is ``profile=default`` appearing in
    ``/opt/data/logs/container-boot.log``, which the 02-reconcile-profiles
    cont-init script writes on every boot. That log entry fires AFTER
    stage2-hook.sh completes, so by the time it appears the full
    cont-init chain (UID remap, chown, config seeding, skills sync,
    browser discovery, config migration) has run.

    Raises ``TimeoutError`` if the container never becomes ready — much
    better than a fixed ``time.sleep()`` that either wastes time on fast
    machines or flakes on slow ones.
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        r = docker_exec(
            container,
            "sh", "-c",
            "cat /opt/data/logs/container-boot.log 2>/dev/null",
            timeout=5,
        )
        if r.returncode == 0 and "profile=default" in r.stdout:
            return
        time.sleep(interval_s)
    raise TimeoutError(
        f"container {container} did not finish cont-init within {deadline_s}s"
    )


def start_container(
    image: str,
    name: str,
    *env: str,
    cmd: str = "sleep infinity",
    timeout: int = 60,
) -> str:
    """Start a detached container and wait for cont-init to finish.

    Args:
        image: Docker image to run.
        name: Container name (cleanup is the caller's responsibility —
            typically handled by the ``container_name`` fixture).
        env: Env vars as ``KEY=VALUE`` strings, each passed via ``-e``.
        cmd: Container CMD (default ``sleep infinity``).
        timeout: ``docker run`` subprocess timeout.

    Returns the container name. Raises on ``docker run`` failure or if
    the container never finishes cont-init within 30s.
    """
    register_container(name)
    args = ["docker", "run", "-d", "--name", name]
    for e in env:
        args.extend(["-e", e])
    args.extend([image, *cmd.split()])
    subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    wait_for_container_ready(name)
    return name


def restart_container(container: str, timeout: int = 60) -> None:
    """Restart a container and wait for cont-init to finish.

    Equivalent to ``docker restart <container>`` followed by
    :func:`wait_for_container_ready`.

    The readiness signal (``profile=default`` in
    ``/opt/data/logs/container-boot.log``) is append-only and persists
    across restarts, so we truncate it BEFORE restarting — otherwise
    ``wait_for_container_ready`` would match the stale line from the
    previous boot and return before cont-init runs on the new boot.
    """
    docker_exec(container, "sh", "-c",
                "truncate -s 0 /opt/data/logs/container-boot.log 2>/dev/null || true",
                user="root", timeout=5)
    subprocess.run(
        ["docker", "restart", container],
        check=True, capture_output=True, timeout=timeout,
    )
    wait_for_container_ready(container)


def poll_container(
    container: str,
    probe: str,
    *,
    deadline_s: float = 30.0,
    interval_s: float = 0.5,
    user: str = "hermes",
) -> tuple[bool, str]:
    """Repeatedly run ``probe`` inside the container until it exits 0 or
    ``deadline_s`` elapses.

    Returns ``(success, last_stdout)``. Useful for waiting on a process
    to appear, a port to open, a file to contain a string, etc.
    """
    end = time.monotonic() + deadline_s
    last = ""
    while time.monotonic() < end:
        r = docker_exec_sh(container, probe, user=user, timeout=10)
        last = r.stdout
        if r.returncode == 0:
            return True, last
        time.sleep(interval_s)
    return False, last


def wait_for_path(
    container: str,
    path: str,
    *,
    kind: str = "f",
    deadline_s: float = 30.0,
    interval_s: float = 0.25,
) -> bool:
    """Poll ``test -<kind> <path>`` inside the container until success or timeout.

    ``kind`` is the ``test`` flag: ``'f'`` for file, ``'d'`` for directory,
    ``'e'`` for existence. Returns ``True`` on success, ``False`` on timeout.
    """
    return poll_container(
        container, f"test -{kind} {path}",
        deadline_s=deadline_s, interval_s=interval_s,
    )[0]


def wait_for_log(
    container: str,
    log_path: str,
    needle: str,
    *,
    deadline_s: float = 30.0,
    interval_s: float = 0.25,
) -> str:
    """Poll until a log file inside the container contains ``needle``.

    Returns the full log on success.
    """
    end = time.monotonic() + deadline_s
    last = ""
    while time.monotonic() < end:
        r = docker_exec_sh(
            container, f"cat {log_path} 2>/dev/null", timeout=5,
        )
        if r.returncode == 0:
            last = r.stdout
            if needle in last:
                return last
        time.sleep(interval_s)
    raise AssertionError(f"Didn't see `{needle}` in {log_path} within {deadline_s} in container {container}")



def wait_for_docker_logs(
    container: str, needle: str, *, deadline_s: float = 30.0, interval_s: float = 0.5,
) -> str:
    """Poll ``docker logs`` until ``needle`` appears or deadline expires.

    Returns the full docker logs on success.
    """
    end = time.monotonic() + deadline_s
    last = ""
    while time.monotonic() < end:
        r = subprocess.run(
            ["docker", "logs", container],
            capture_output=True, text=True, timeout=10,
        )
        last = r.stdout + r.stderr
        if needle in last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"Didn't see `{needle}` in docker logs within {deadline_s} in container {container}")
