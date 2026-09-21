"""The Docker command environment follows the current turn without contacting Docker."""
from contextvars import Context

from gateway.session_context import _UNSET, _VAR_MAP, clear_session_vars, set_session_vars
from tools.environments.docker import DockerEnvironment


def test_context_identity_wins_over_process_passthrough(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-process-session")
    env = object.__new__(DockerEnvironment)
    env._forward_env = ["HERMES_SESSION_ID"]

    def resolve(session_id, chat_id):
        tokens = set_session_vars(session_id=session_id, chat_id=chat_id, platform="telegram")
        try:
            values, unset = env._resolve_passthrough_env()
            assert values["HERMES_SESSION_ID"] == session_id
            assert values["HERMES_SESSION_CHAT_ID"] == chat_id
            assert values["HERMES_SESSION_PLATFORM"] == "telegram"
            assert not set(values) & unset
        finally:
            clear_session_vars(tokens)

    for session_id, chat_id in (("first", "11"), ("second", "22"), ("first", "11")):
        Context().run(resolve, session_id, chat_id)


def test_engaged_context_unsets_identity_missing_from_this_turn(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "previous-user")
    env = object.__new__(DockerEnvironment)
    env._forward_env = ["HERMES_SESSION_USER_ID"]

    def resolve():
        tokens = set_session_vars(session_id="new-session")
        try:
            values, unset = env._resolve_passthrough_env()
            assert values["HERMES_SESSION_USER_ID"] == ""
            assert values["HERMES_SESSION_ID"] == "new-session"
            user_var = _VAR_MAP["HERMES_SESSION_USER_ID"]
            user_token = user_var.set(_UNSET)
            try:
                values, unset = env._resolve_passthrough_env()
                assert "HERMES_SESSION_USER_ID" not in values
                assert "HERMES_SESSION_USER_ID" in unset
            finally:
                user_var.reset(user_token)
        finally:
            clear_session_vars(tokens)

    Context().run(resolve)
