"""Regression coverage for native-vision tool payload lifecycle."""

from __future__ import annotations

import json

from agent.tool_dispatch_helpers import (
    compact_consumed_tool_media,
    make_tool_result_message,
)


def native_vision_result(marker: str, size: int = 512_000) -> dict:
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": f"inspect {marker}"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + (marker * size)}},
        ],
        "text_summary": f"native image {marker}",
        "meta": {
            "native_vision": True,
            "file_reference": f"/tmp/{marker}.png",
            "width": 1080,
            "height": 1350,
            "content_sha256": marker * 64,
            "size_bytes": size,
        },
    }


def append_completed_vision(messages: list[dict], marker: str) -> None:
    result = native_vision_result(marker)
    messages.extend([
        {"role": "assistant", "tool_calls": [{"id": marker}]},
        make_tool_result_message(
            "vision_analyze",
            result["content"],
            marker,
            source_result=result,
        ),
        {"role": "assistant", "content": f"analysis {marker}"},
    ])


def test_consumed_native_images_are_compacted_to_linear_metadata_history():
    messages: list[dict] = []
    serialized_sizes = []
    for marker in ("a", "b", "c"):
        append_completed_vision(messages, marker)
        assert compact_consumed_tool_media(messages) == 1
        serialized_sizes.append(len(json.dumps(messages)))

    assert serialized_sizes[-1] < 10_000
    assert serialized_sizes[-1] < serialized_sizes[0] * 4
    assert all("base64," not in json.dumps(message) for message in messages)
    compact_record = json.loads(messages[1]["content"])
    assert compact_record["media"]["fileReference"] == "/tmp/a.png"
    assert compact_record["media"]["width"] == 1080
    assert compact_record["media"]["contentSha256"] == "a" * 64
    assert messages[2]["content"] == "analysis a"


def test_pending_native_image_remains_available_for_its_immediate_model_call():
    result = native_vision_result("z")
    message = make_tool_result_message(
        "vision_analyze",
        result["content"],
        "z",
        source_result=result,
    )
    messages = [{"role": "assistant", "tool_calls": [{"id": "z"}]}, message]

    assert compact_consumed_tool_media(messages) == 0
    assert "base64," in json.dumps(messages)
