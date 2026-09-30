"""``fb:`` button taps go to the deployment's ``telegram_feedback`` module, never the model."""

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.platforms.telegram.adapter import TelegramAdapter


class _TestTelegramAdapter(TelegramAdapter):
    name = "test"


def _adapter():
    adapter = object.__new__(_TestTelegramAdapter)
    adapter._accept_update = lambda: None
    return adapter


def _query(data):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=7, first_name="Reviewer"),
        message=SimpleNamespace(chat_id=11, chat=SimpleNamespace(type="private"), message_thread_id=None),
        answer=AsyncMock(),
    )


def test_feedback_tap_is_handed_to_the_installed_module(monkeypatch):
    received = []

    async def record_feedback(adapter, query):
        received.append((adapter, query))

    module = types.ModuleType("telegram_feedback")
    module.record_feedback = record_feedback
    monkeypatch.setitem(sys.modules, "telegram_feedback", module)
    adapter = _adapter()
    adapter._handle_review_helper_callback = AsyncMock()
    adapter._handle_model_picker_callback = AsyncMock()
    query = _query("fb:0f9c7f7e-4b1e-4c55-9a55-5f0c1d2e3f40:funny")

    asyncio.run(adapter._handle_callback_query(SimpleNamespace(callback_query=query), None))

    assert received == [(adapter, query)]
    adapter._handle_review_helper_callback.assert_not_awaited()
    adapter._handle_model_picker_callback.assert_not_awaited()


def test_feedback_tap_without_a_module_is_answered_and_dropped(monkeypatch):
    monkeypatch.setitem(sys.modules, "telegram_feedback", None)
    query = _query("fb:0f9c7f7e-4b1e-4c55-9a55-5f0c1d2e3f40:weak")

    asyncio.run(_adapter()._handle_callback_query(SimpleNamespace(callback_query=query), None))

    query.answer.assert_awaited_once_with(text="Feedback is not available.")
