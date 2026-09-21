"""Candidate metadata reaches the active final-response and post-stream implementations."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("marker,key", [("postgen_candidate_id", "postgen_candidate"), ("review_candidate_id", "review_candidate")])
@pytest.mark.parametrize("streamed", [False, True])
async def test_final_delivery_preserves_candidate_and_topic(tmp_path, monkeypatch, marker, key, streamed):
    image = tmp_path / "candidate.png"
    image.write_bytes(b"image")
    monkeypatch.setattr("gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path,))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", typing_indicator=False))
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="text"))
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True, message_id="photo"))
    response = f"Candidate\n[[{marker}:candidate_123]]\nMEDIA:{image}"
    adapter._message_handler = AsyncMock(return_value=response)
    event = MessageEvent(
        text="make a candidate", message_type=MessageType.TEXT, message_id="41",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="456", chat_type="group", thread_id="17"))
    if streamed:
        await GatewayNotificationsMixin._deliver_media_from_response(
            SimpleNamespace(), response, event, adapter, thread_metadata={"thread_id": "17"})
    else:
        await adapter._process_message_background(event, "candidate-session")
        for call in adapter.send.await_args_list:
            assert "[[" not in call.kwargs.get("content", "")
    adapter.send_multiple_images.assert_awaited_once()
    sent = adapter.send_multiple_images.await_args.kwargs
    assert sent["metadata"][key] == {"id": "candidate_123"}
    assert sent["metadata"]["thread_id"] == "17"
    assert str(image) in sent["images"][0][0]
