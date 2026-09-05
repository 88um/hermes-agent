"""Per-candidate Postgen delivery through ``send_message``.

The gateway's final-response path carries one ``[[postgen_candidate_id:...]]`` marker per turn,
so a turn that prepares several candidates could give only one of them its buttons and its
platform message id. Sending one candidate's exact gateway result through ``send_message`` is the
operation that closes that: the marker is resolved by the same registry helper the gateway uses,
a carousel travels as one album followed by the adapter's own control card, and the result names
every message with the slide it shows.

The ``telegram`` package is mocked with the same helper shape as
``tests/gateway/test_postgen_candidate_buttons.py`` (a real InlineKeyboardButton/Markup pair, and
the ``telegram.ext`` / ``telegram.constants`` / ``telegram.request`` attributes the adapter
imports), because the reply-markup helper really instantiates those classes.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


class _InputMediaPhoto:
    def __init__(self, media=None, caption=None, **kwargs):
        self.media = media
        self.caption = caption


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.Update = object
    mod.Message = object
    mod.InlineKeyboardButton = _InlineKeyboardButton
    mod.InlineKeyboardMarkup = _InlineKeyboardMarkup
    mod.InputMediaPhoto = _InputMediaPhoto
    mod.LinkPreviewOptions = object
    mod.MessageEntity = lambda **kw: SimpleNamespace(**kw)
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
    return mod


_TELEGRAM = _ensure_telegram_mock()

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_bot(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A Bot whose sends answer with increasing message ids."""
    bot = MagicMock()
    counter = {"next": 1}

    def _message(**_kwargs):
        counter["next"] += 1
        return SimpleNamespace(message_id=counter["next"])

    bot.send_message = AsyncMock(side_effect=lambda **kw: _message(**kw))
    bot.send_photo = AsyncMock(side_effect=lambda **kw: _message(**kw))
    bot.send_media_group = AsyncMock(
        side_effect=lambda **kw: [_message() for _ in kw.get("media", [])]
    )
    if _TELEGRAM is not None:
        monkeypatch.setattr(_TELEGRAM, "Bot", MagicMock(return_value=bot), raising=False)
    return bot


def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TELEGRAM_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
        "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None, raising=False)
    monkeypatch.setattr(
        "gateway.platforms.base._detect_macos_system_proxy", lambda: None, raising=False
    )


def _stub_registry(monkeypatch: pytest.MonkeyPatch, row) -> None:
    """The candidate registry answers for this id without running the helper script."""
    monkeypatch.setattr(
        TelegramAdapter, "_resolve_postgen_candidate", lambda self, candidate_id: row
    )
    monkeypatch.setattr(
        TelegramAdapter, "_log_postgen_candidate_delivery", lambda self, *a, **kw: None
    )


def _tmpfile(suffix: str = ".png") -> str:
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(b"x")
    handle.close()
    return handle.name


def _pconfig():
    return SimpleNamespace(token="tok", enabled=True, extra={})


def _send(**kwargs):
    from tools.send_message_tool import _send_telegram

    return asyncio.run(_send_telegram("tok", "123", kwargs.pop("message", ""), **kwargs))


def test_single_candidate_carries_its_buttons_and_reports_its_message(monkeypatch):
    _no_proxy(monkeypatch)
    bot = _make_bot(monkeypatch)
    _stub_registry(monkeypatch, {
        "id": "gta6q1", "candidate_shape": "single", "media_count": 1, "status": "registered",
    })

    result = _send(
        media_files=[(_tmpfile(), False)],
        platform_config=_pconfig(),
        postgen_candidate={"id": "gta6q1"},
    )

    assert result["success"] is True
    assert result["postgen_candidate_id"] == "gta6q1"
    assert result["buttons_attached"] is True
    assert result["carousel_album"] is False
    assert result["bindings"] == [{"message_id": result["message_ids"][0], "slide_index": None}]
    markup = bot.send_photo.await_args.kwargs["reply_markup"]
    assert isinstance(markup, _InlineKeyboardMarkup)


def test_two_candidates_are_two_sends_with_their_own_ids(monkeypatch):
    _no_proxy(monkeypatch)
    _make_bot(monkeypatch)
    seen = []
    for candidate_id in ("aaa111", "bbb222"):
        _stub_registry(monkeypatch, {
            "id": candidate_id, "candidate_shape": "single", "media_count": 1,
            "status": "registered",
        })
        result = _send(
            media_files=[(_tmpfile(), False)],
            platform_config=_pconfig(),
            postgen_candidate={"id": candidate_id},
        )
        assert result["postgen_candidate_id"] == candidate_id
        seen.append(result["message_ids"])

    assert seen[0] != seen[1], "each candidate reports its own platform message"


def test_a_carousel_is_one_album_plus_the_control_card(monkeypatch):
    _no_proxy(monkeypatch)
    bot = _make_bot(monkeypatch)
    _stub_registry(monkeypatch, {
        "id": "car001", "candidate_shape": "carousel", "media_count": 3, "status": "registered",
    })

    result = _send(
        media_files=[(_tmpfile(), False), (_tmpfile(), False), (_tmpfile(), False)],
        platform_config=_pconfig(),
        postgen_candidate={"id": "car001"},
    )

    bot.send_media_group.assert_awaited_once()
    assert result["carousel_album"] is True
    # Telegram carries no keyboard on a media group, so the album is followed by the adapter's
    # own control card — which is where the buttons really are.
    assert result["buttons_attached"] is True
    slides = [binding["slide_index"] for binding in result["bindings"]]
    assert slides == [1, 2, 3, None]
    assert result["bindings"][-1]["control"] is True
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


def test_a_partial_carousel_is_refused_rather_than_delivered(monkeypatch):
    _no_proxy(monkeypatch)
    bot = _make_bot(monkeypatch)
    _stub_registry(monkeypatch, {
        "id": "car002", "candidate_shape": "carousel", "media_count": 3, "status": "registered",
    })

    result = _send(
        media_files=[(_tmpfile(), False), (_tmpfile(), False)],
        platform_config=_pconfig(),
        postgen_candidate={"id": "car002"},
    )

    assert "error" in result
    assert "postgen_partial_carousel_refused" in result["error"]
    bot.send_media_group.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


def test_an_unresolvable_candidate_sends_plainly_with_no_buttons(monkeypatch):
    _no_proxy(monkeypatch)
    bot = _make_bot(monkeypatch)
    _stub_registry(monkeypatch, None)

    result = _send(
        media_files=[(_tmpfile(), False)],
        platform_config=_pconfig(),
        postgen_candidate={"id": "goneid"},
    )

    assert result["success"] is True
    assert "postgen_candidate_id" not in result
    assert "reply_markup" not in bot.send_photo.await_args.kwargs
