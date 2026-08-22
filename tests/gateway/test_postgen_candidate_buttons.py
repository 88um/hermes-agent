import base64
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


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.Update = object
    mod.Bot = object
    mod.Message = object
    mod.InlineKeyboardButton = _InlineKeyboardButton
    mod.InlineKeyboardMarkup = _InlineKeyboardMarkup
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

from gateway.platforms.base import BasePlatformAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter


def _directive(payload):
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"[[postgen_candidate:{encoded}]]"


def test_extract_postgen_candidate_metadata_decodes_and_strips_directive():
    payload = {
        "id": "cand123",
        "request_key": "req123",
        "result_path": "/tmp/result.json",
        "postgen_theme": "red format",
        "headline_label": "short label",
        "artifact_kind": "candidate",
        "gate_record_path": "/tmp/gate.json",
        "gate_class": "full_visual",
        "artifact_sha256": "a" * 64,
        "prompt_version": "hostile-visual-qa-v1",
        "compare_status": "pass",
        "qa_verdict": "Ship",
    }
    text = f"Made it.\n{_directive(payload)}\nMEDIA:/tmp/post.png"

    candidate, cleaned = BasePlatformAdapter.extract_postgen_candidate_metadata(text)

    assert candidate == payload
    assert "postgen_candidate" not in cleaned
    assert "MEDIA:/tmp/post.png" in cleaned


def test_telegram_postgen_candidate_keyboard_uses_short_callback_payloads(monkeypatch):
    import plugins.platforms.telegram.adapter as telegram_adapter

    monkeypatch.setattr(telegram_adapter, "InlineKeyboardButton", _InlineKeyboardButton)
    monkeypatch.setattr(telegram_adapter, "InlineKeyboardMarkup", _InlineKeyboardMarkup)
    adapter = object.__new__(TelegramAdapter)
    keyboard = adapter._postgen_candidate_reply_markup({
        "postgen_candidate": {"id": "cand123"}
    })

    rows = keyboard.inline_keyboard
    assert [button.text for button in rows[0]] == ["✅ Approve", "❌ Reject"]
    assert [button.text for button in rows[1]] == ["✏️ Revise"]
    callback_data = [button.callback_data for row in rows for button in row]
    assert callback_data == ["pg:a:cand123", "pg:r:cand123", "pg:v:cand123"]
    assert all(len(data.encode("utf-8")) <= 64 for data in callback_data)
