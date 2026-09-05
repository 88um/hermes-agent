"""A persistent sandbox whose task id is a colon-separated session key.

``-v host:container`` is parsed by splitting on ``:``, so the persistent sandbox
directory — ``get_sandbox_dir()/docker/<task_id>``, where the task id *is* the session
key — produced::

    docker: invalid spec: /…/docker/session:agent:main:telegram:dm:1066149310/home:/root:
    too many colons

and every such sandbox failed to start with exit 125. The mounts are ``--mount`` now,
which places no restriction on the host path.
"""

import subprocess

from tools.environments import docker as docker_env

from tests.tools.test_docker_environment import _make_dummy_env, _mock_subprocess_run

COLON_TASK_ID = "session:agent:main:telegram:dm:1066149310"


def _run_args(calls):
    for cmd, _kwargs in calls:
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "run":
            return cmd
    raise AssertionError("no `docker run` was issued")


def test_persistent_sandbox_with_a_colon_bearing_task_id_uses_mount(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _mock_subprocess_run(monkeypatch)

    _make_dummy_env(task_id=COLON_TASK_ID, persistent_filesystem=True)
    args = _run_args(calls)

    # No `-v` in this command may carry a host path with a colon in it: that is exactly
    # the spec Docker refuses.
    for index, argument in enumerate(args):
        if argument == "-v":
            spec = args[index + 1]
            host = spec.split(":")[0]
            assert ":" not in host, f"-v {spec} would be rejected as `too many colons`"
            assert spec.count(":") <= 2, f"-v {spec} has too many colons"

    mounts = [args[index + 1] for index, argument in enumerate(args) if argument == "--mount"]
    home = [mount for mount in mounts if mount.endswith("dst=/root")]
    assert home, f"the sandbox home is bind-mounted with --mount; got {mounts}"
    assert COLON_TASK_ID in home[0]
    assert home[0].startswith("type=bind,src=")


def test_bind_mount_args_shape():
    assert docker_env.bind_mount_args("/a:b/c", "/root") == [
        "--mount", "type=bind,src=/a:b/c,dst=/root",
    ]
    assert docker_env.bind_mount_args("/a", "/b", readonly=True) == [
        "--mount", "type=bind,src=/a,dst=/b,readonly",
    ]


def test_a_comma_in_a_host_path_is_refused_rather_than_mangled():
    # --mount splits its own options on commas and Docker has no escape for one, so this path
    # would silently become `src=/a` plus a bogus `b/home` option.
    try:
        docker_env.bind_mount_args("/a,b/home", "/root")
    except ValueError as error:
        assert "comma" in str(error)
        assert "/a,b/home" in str(error)
    else:
        raise AssertionError("a comma-bearing src must be refused")

    try:
        docker_env.bind_mount_args("/a/home", "/root,x")
    except ValueError as error:
        assert "comma" in str(error)
    else:
        raise AssertionError("a comma-bearing dst must be refused")


def test_an_equals_sign_in_a_path_is_representable():
    # Only the first `=` separates the key from its value, so this is unambiguous.
    assert docker_env.bind_mount_args("/a=b/home", "/root") == [
        "--mount", "type=bind,src=/a=b/home,dst=/root",
    ]
