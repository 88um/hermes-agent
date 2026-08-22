"""Native image identity survives the existing text-only history projections."""

from __future__ import annotations

import copy
import json

from agent.context_compressor import evict_stale_outbound_tool_images
from agent.image_eviction_policy import OUTBOUND_IMAGE_LIMIT
from agent.session_persistence import _durable_content
from agent.tool_dispatch_helpers import make_tool_result_message
from tools.vision_tools import _build_native_vision_tool_result


def native_vision_result(marker: str) -> dict:
    return _build_native_vision_tool_result(
        image_url=f"/tmp/{marker}.png",
        question=f"inspect {marker}",
        image_data_url="data:image/png;base64," + marker * 512,
        image_size_bytes=384,
        width=1080,
        height=1350,
        content_sha256=marker * 64,
    )


def media_identity(text: str) -> dict:
    return json.loads(text.split("Image metadata: ", 1)[1].split("\n", 1)[0])


def test_pending_image_stays_live_while_durable_projection_keeps_only_identity():
    result = native_vision_result("a")
    original = copy.deepcopy(result)
    message = make_tool_result_message("vision_analyze", result["content"], "a")
    messages = [{"role": "assistant", "tool_calls": [{"id": "a"}]}, message]

    assert evict_stale_outbound_tool_images(messages) == 0
    for content in (result, message["content"]):
        durable = _durable_content(content)
        assert "base64," not in durable
        assert media_identity(durable) == {
            "fileReference": "/tmp/a.png",
            "sizeBytes": 384,
            "width": 1080,
            "height": 1350,
            "contentSha256": "a" * 64,
        }
    assert result == original
    assert "base64," in json.dumps(message["content"])


def test_outbound_eviction_keeps_identity_without_mutating_cached_history():
    history = []
    for index in range(OUTBOUND_IMAGE_LIMIT + 1):
        marker = str(index)
        result = native_vision_result(marker)
        history.extend([
            {"role": "assistant", "tool_calls": [{"id": marker}]},
            make_tool_result_message("vision_analyze", result["content"], marker),
            {"role": "assistant", "content": f"analysis {marker}"},
        ])
    original = copy.deepcopy(history)
    outbound = copy.deepcopy(history)

    assert evict_stale_outbound_tool_images(outbound) > 0
    assert "base64," not in json.dumps(outbound[1]["content"])
    assert media_identity(_durable_content(outbound[1]["content"])) == media_identity(
        _durable_content(history[1]["content"])
    )
    assert outbound[2]["content"] == "analysis 0"
    assert "base64," in json.dumps(outbound[-2]["content"])
    assert history == original
