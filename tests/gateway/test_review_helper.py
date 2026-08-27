import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


class _InlineKeyboardButton:
    def __init__(self, text, callback_data=None, **kwargs):
        self.text = text
        self.callback_data = callback_data


class _InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class _ForceReply:
    def __init__(self, selective=False, **kwargs):
        self.selective = selective


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.Update = object
    mod.Bot = object
    mod.Message = object
    mod.InlineKeyboardButton = _InlineKeyboardButton
    mod.InlineKeyboardMarkup = _InlineKeyboardMarkup
    mod.ForceReply = _ForceReply
    mod.LinkPreviewOptions = object
    mod.ext.Application = object
    mod.ext.CommandHandler = object
    mod.ext.CallbackQueryHandler = object
    mod.ext.MessageHandler = object
    mod.ext.ContextTypes.DEFAULT_TYPE = object
    mod.ext.filters = SimpleNamespace()
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.request.HTTPXRequest = object
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules[name] = mod


_ensure_telegram_mock()

import pytest

from gateway.review_helper import (
    REVIEW_CALLBACK_MAX_BYTES,
    ReviewHelperClient,
    ReviewHelperConfig,
    callback_data,
    extract_review_candidate_metadata,
    sanitize_candidate_payload,
    valid_candidate_id,
)
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.adapter import _apply_yaml_config


CANDIDATE_ROW = {"id": "candidate_123", "status": "pending", "lane": "near"}


@pytest.fixture
def adapter(monkeypatch):
    adapter_module = __import__(
        "plugins.platforms.telegram.adapter", fromlist=["InlineKeyboardButton"]
    )
    monkeypatch.setattr(adapter_module, "InlineKeyboardButton", _InlineKeyboardButton)
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", _InlineKeyboardMarkup)
    monkeypatch.setattr(adapter_module, "ForceReply", _ForceReply)
    instance = object.__new__(TelegramAdapter)
    instance._reply_to_mode = "off"
    instance._notifications_mode = "important"
    instance.config = SimpleNamespace(extra={})
    instance._review_note_prompts = {}
    return instance


def test_marker_is_short_and_stripped_for_canonical_and_early_alias():
    for marker in (
        "[[review_candidate_id:candidate_123]]",
        "[[review_candidate:candidate_123]]",
    ):
        candidate, cleaned = extract_review_candidate_metadata(
            f"Headline\n{marker}\nMEDIA:/tmp/final.png"
        )
        assert candidate == {"id": "candidate_123"}
        assert cleaned == "Headline\n\nMEDIA:/tmp/final.png"


def test_marker_fails_closed_for_malformed_or_unterminated_directives(caplog):
    for text in (
        "[[review_candidate_id:]]",
        f"[[review_candidate_id:{'x' * 49}]]",
        "[[review_candidate_id:../../secret]]",
        "before\n[[review_candidate_id:candidate_123\nMEDIA:/tmp/x.png",
    ):
        candidate, cleaned = extract_review_candidate_metadata(text)
        assert candidate is None
        assert "[[review_candidate" not in cleaned
    assert any("review_candidate_directive_invalid" in rec.message for rec in caplog.records)


def test_candidate_id_and_payload_never_accept_model_paths():
    assert valid_candidate_id("candidate_123")
    assert not valid_candidate_id("/tmp/secret")
    assert sanitize_candidate_payload(
        {
            "id": "candidate_123",
            "path": "/tmp/secret",
            "executable": "/tmp/evil",
        }
    ) == {"id": "candidate_123"}


def test_callback_lane_is_bounded_for_max_id_and_reason():
    candidate_id = "x" * 48
    values = [
        callback_data("f", candidate_id),
        callback_data("w", candidate_id),
        callback_data("b", candidate_id),
        callback_data("r", candidate_id),
        callback_data("reason", candidate_id, "ot"),
    ]
    assert all(value is not None for value in values)
    assert all(len(value.encode("utf-8")) <= REVIEW_CALLBACK_MAX_BYTES for value in values)
    assert callback_data("reason:bad", candidate_id) is None


def test_profile_config_requires_absolute_executable_and_keeps_static_args():
    assert ReviewHelperConfig.from_config(
        {"review_helper": {"executable": "review-helper"}}
    ) is None
    config = ReviewHelperConfig.from_config(
        {
            "extra": {
                "review_helper": {
                    "executable": "/opt/review-helper",
                    "args": ["--profile", "humorbank"],
                    "note_max_bytes": 123,
                }
            }
        }
    )
    assert config is not None
    assert config.command("event") == [
        "/opt/review-helper", "--profile", "humorbank", "event"
    ]
    assert config.note_max_bytes == 123
    assert config.command("shell") is None


def test_yaml_bridge_keeps_helper_identity_in_profile_extra_not_environment(monkeypatch):
    monkeypatch.delenv("REVIEW_HELPER_EXECUTABLE", raising=False)
    helper = {"enabled": True, "executable": "/opt/review-helper", "args": ["--profile", "h"]}
    extras = _apply_yaml_config({}, {"review_helper": helper})
    assert extras["review_helper"] == helper
    assert "REVIEW_HELPER_EXECUTABLE" not in __import__("os").environ


def test_helper_uses_profile_argv_and_compact_stdin(monkeypatch):
    config = ReviewHelperConfig(executable=sys.executable, args=("--flag",))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true}', stderr=b"")

    import gateway.review_helper as review_module

    monkeypatch.setattr(review_module.subprocess, "run", fake_run)
    result = ReviewHelperClient(config).invoke(
        "resolve", {"id": "candidate_123", "path": "/tmp/model-path"}
    )
    assert result == {"ok": True}
    assert calls[0][0] == [sys.executable, "--flag", "resolve"]
    assert json.loads(calls[0][1]["input"].decode("utf-8")) == {
        "id": "candidate_123",
        "path": "/tmp/model-path",
    }
    assert calls[0][1].get("shell", False) is False


def test_candidate_keyboard_has_four_actions(adapter):
    keyboard = adapter._review_candidate_reply_markup(CANDIDATE_ROW)
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == [
        "😂 Funny", "😐 Weak", "❌ Bad", "♻️ Repost"
    ]
    assert [button.callback_data for button in buttons] == [
        "rh:f:candidate_123",
        "rh:w:candidate_123",
        "rh:b:candidate_123",
        "rh:r:candidate_123",
    ]


def test_text_candidate_delivery_attaches_keyboard_and_delivery_event(adapter, monkeypatch):
    captured = {}

    async def send_message(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(message_id=17)

    adapter._bot = SimpleNamespace(send_message=send_message)
    adapter._resolve_review_candidate = lambda candidate_id: CANDIDATE_ROW
    adapter._should_attempt_rich = lambda *args, **kwargs: False
    adapter.format_message = lambda text: text
    adapter.truncate_message = lambda text, *args, **kwargs: [text]
    adapter._metadata_thread_id = lambda metadata: None
    adapter._message_thread_id_for_send = lambda thread_id: None
    adapter._is_private_dm_topic_send = lambda *args: False
    adapter._should_thread_reply = lambda *args: False
    adapter._thread_kwargs_for_send = lambda *args, **kwargs: {}
    adapter._link_preview_kwargs = lambda: {}
    adapter.send_typing = lambda *args, **kwargs: asyncio.sleep(0)
    delivery = []
    adapter._log_review_candidate_delivery = lambda *args, **kwargs: delivery.append((args, kwargs))

    result = asyncio.run(
        adapter.send(
            "123",
            "A candidate",
            metadata={"review_candidate": {"id": "candidate_123"}},
        )
    )
    assert result.success
    assert captured["reply_markup"].inline_keyboard[0][0].callback_data == "rh:f:candidate_123"
    assert delivery and delivery[0][1]["attached"] is True


def test_media_candidate_delivery_attaches_keyboard(adapter, monkeypatch, tmp_path):
    image_path = tmp_path / "candidate.png"
    image_path.write_bytes(b"png")
    captured = {}

    async def send_photo(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(message_id=22)

    async def send_with_retry(method, kwargs, *args, **extra):
        return await method(**kwargs)

    adapter._bot = SimpleNamespace(send_photo=send_photo)
    adapter._resolve_review_candidate = lambda candidate_id: CANDIDATE_ROW
    adapter._send_with_dm_topic_reply_anchor_retry = send_with_retry
    adapter._metadata_thread_id = lambda metadata: None
    adapter._reply_to_message_id_for_send = lambda *args, **kwargs: None
    adapter._thread_kwargs_for_send = lambda *args, **kwargs: {}
    delivery = []
    adapter._log_review_candidate_delivery = lambda *args, **kwargs: delivery.append((args, kwargs))

    result = asyncio.run(
        adapter.send_image_file(
            "123", str(image_path), metadata={"review_candidate": {"id": "candidate_123"}}
        )
    )
    assert result.success
    assert captured["reply_markup"].inline_keyboard[0][3].callback_data == "rh:r:candidate_123"
    assert delivery and delivery[0][1]["attached"] is True


class _FakeQuery:
    def __init__(self, data, message_id=99, user_id="operator"):
        self.data = data
        self.message = SimpleNamespace(
            message_id=message_id,
            chat_id="123",
            chat=SimpleNamespace(type="private"),
            text="candidate",
        )
        self.from_user = SimpleNamespace(id=user_id, first_name="Operator")
        self.answers = []
        self.edits = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)


def test_weak_and_bad_record_verdict_before_reason_keyboard(adapter):
    events = []
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter._resolve_review_candidate = lambda candidate_id: CANDIDATE_ROW
    adapter._invoke_review_helper = lambda operation, payload: events.append((operation, payload)) or {"ok": True}

    query = _FakeQuery("rh:w:candidate_123")
    asyncio.run(
        adapter._handle_review_helper_callback(
            query, query.data, query_chat_id="123", query_chat_type="private"
        )
    )
    assert events[0][1]["verdict"] == "weak"
    assert len(query.edits) == 1
    reason_buttons = [button for row in query.edits[0]["reply_markup"].inline_keyboard for button in row]
    assert len(reason_buttons) == 8
    assert any("Other / add note" == button.text for button in reason_buttons)


def test_reason_and_add_note_append_reason_then_send_bound_prompt(adapter):
    events = []
    prompts = []
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter._resolve_review_candidate = lambda candidate_id: CANDIDATE_ROW
    adapter._invoke_review_helper = lambda operation, payload: events.append((operation, payload)) or {"ok": True}
    async def send_note_prompt(*args, **kwargs):
        prompts.append(kwargs)

    adapter._send_review_note_prompt = send_note_prompt
    query = _FakeQuery("rh:reason:candidate_123:ot")
    asyncio.run(
        adapter._handle_review_helper_callback(
            query, query.data, query_chat_id="123", query_chat_type="private"
        )
    )
    assert events[0][1]["event_type"] == "reason"
    assert events[0][1]["reason"] == "other_add_note"
    assert prompts and prompts[0]["candidate_id"] == "candidate_123"


def test_duplicate_tap_is_idempotent_and_unknown_candidate_fails_closed(adapter):
    events = []
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter._resolve_review_candidate = lambda candidate_id: CANDIDATE_ROW
    adapter._invoke_review_helper = lambda operation, payload: events.append(payload) or {"ok": False, "reason": "already_recorded"}
    duplicate = _FakeQuery("rh:f:candidate_123")
    asyncio.run(
        adapter._handle_review_helper_callback(
            duplicate, duplicate.data, query_chat_id="123", query_chat_type="private"
        )
    )
    assert len(events) == 1
    assert not duplicate.edits
    assert duplicate.answers[-1]["text"] == "Already recorded."

    adapter._resolve_review_candidate = lambda candidate_id: None
    unknown = _FakeQuery("rh:b:ghost")
    asyncio.run(
        adapter._handle_review_helper_callback(
            unknown, unknown.data, query_chat_id="123", query_chat_type="private"
        )
    )
    assert len(events) == 1
    assert unknown.answers[-1]["text"] == "This candidate is unavailable."


def test_unauthorized_callback_fails_closed_without_helper_call(adapter):
    called = []
    adapter._is_callback_user_authorized = lambda *args, **kwargs: False
    adapter._invoke_review_helper = lambda *args, **kwargs: called.append(True) or {"ok": True}
    query = _FakeQuery("rh:f:candidate_123", user_id="intruder")
    asyncio.run(
        adapter._handle_review_helper_callback(
            query, query.data, query_chat_id="123", query_chat_type="private"
        )
    )
    assert not called
    assert "not authorized" in query.answers[-1]["text"]


def test_bound_note_is_consumed_and_utf8_bounded_without_agent_dispatch(adapter):
    calls = []
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter._invoke_review_helper = lambda operation, payload: calls.append((operation, payload)) or {"ok": True}
    adapter._review_note_prompts[("123", "88")] = {
        "candidate_id": "candidate_123"
    }
    message = SimpleNamespace(
        text="😀" * 100,
        message_id=89,
        chat=SimpleNamespace(id=123),
        from_user=SimpleNamespace(id="operator"),
        reply_to_message=SimpleNamespace(message_id=88, text="📝 Reply to this message with a review note for the candidate."),
    )
    assert asyncio.run(adapter._maybe_handle_review_note_reply(message)) is True
    assert calls[0][0] == "note"
    assert calls[0][1]["candidate_id"] == "candidate_123"
    assert len(calls[0][1]["note"].encode("utf-8")) <= 4096
    assert ("123", "88") not in adapter._review_note_prompts


def test_unbound_reply_to_review_prompt_is_consumed_and_not_agent_input(adapter):
    calls = []
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter._invoke_review_helper = lambda operation, payload: calls.append((operation, payload)) or {"ok": False, "reason": "unbound"}
    message = SimpleNamespace(
        text="stale note",
        message_id=89,
        chat=SimpleNamespace(id=123),
        from_user=SimpleNamespace(id="operator"),
        reply_to_message=SimpleNamespace(message_id=88, text="📝 Reply to this message with a review note for the candidate."),
    )
    assert asyncio.run(adapter._maybe_handle_review_note_reply(message)) is True
    assert calls[0][0] == "note"
