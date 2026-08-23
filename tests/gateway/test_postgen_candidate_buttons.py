import asyncio
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

import pytest

from gateway.platforms.base import BasePlatformAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter


CANDIDATE_ROW = {
    "id": "gta6q1",
    "request_key": "gta6-20260823",
    "result_path": "/tmp/artifact/run/result.json",
    "image_path": "/tmp/artifact/run/final.png",
    "postgen_theme": "rap format",
    "headline_label": "rockstar ceo",
    "artifact_kind": "candidate",
    "gate_class": "full_visual",
    "qa_verdict": "Ship",
    "status": "registered",
}


class _TestTelegramAdapter(TelegramAdapter):
    name = "test"


@pytest.fixture
def adapter(monkeypatch):
    adapter_module = __import__("plugins.platforms.telegram.adapter", fromlist=["InlineKeyboardButton"])
    monkeypatch.setattr(adapter_module, "InlineKeyboardButton", _InlineKeyboardButton)
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", _InlineKeyboardMarkup)
    instance = object.__new__(_TestTelegramAdapter)
    instance._reply_to_mode = "off"
    return instance


def test_extract_parses_short_id_marker_and_strips_it_from_text():
    text = "Made it.\n[[postgen_candidate_id:gta6q1]]\nMEDIA:/tmp/post.png"

    candidate, cleaned = BasePlatformAdapter.extract_postgen_candidate_metadata(text)

    assert candidate == {"id": "gta6q1"}
    assert "postgen_candidate" not in cleaned
    assert "MEDIA:/tmp/post.png" in cleaned
    assert cleaned.startswith("Made it.")


def test_extract_strips_legacy_base64_directives_without_producing_metadata(caplog):
    legacy = (
        "[[postgen_candidate:eyJpZCI6Imd0YTZxMSIsInJlc3VsdF9wYXRoIjoiL3RtcC9y"
        "ZXN1bHQuanNvbiJ9]]"
    )

    candidate, cleaned = BasePlatformAdapter.extract_postgen_candidate_metadata(f"Made it.\n{legacy}\nMEDIA:/tmp/post.png")

    assert candidate is None
    assert "postgen_candidate" not in cleaned
    assert any("postgen_candidate_directive_invalid" in rec.message for rec in caplog.records)


def test_extract_strips_ellipsized_directives_without_producing_metadata(caplog):
    truncated = "[[postgen_candidate_id:gta6...redacted...q1]]"

    candidate, cleaned = BasePlatformAdapter.extract_postgen_candidate_metadata(f"Made it.\n{truncated}\nMEDIA:/tmp/post.png")

    assert candidate is None
    assert "postgen_candidate" not in cleaned
    assert any("postgen_candidate_directive_invalid" in rec.message for rec in caplog.records)


def test_extract_strips_empty_and_oversized_ids_without_producing_metadata():
    for marker in ("[[postgen_candidate_id:]]", f"[[postgen_candidate_id:{'a' * 49}]]"):
        candidate, cleaned = BasePlatformAdapter.extract_postgen_candidate_metadata(marker + "\nMEDIA:/tmp/post.png")
        assert candidate is None
        assert "postgen_candidate" not in cleaned


def test_extract_strips_an_unterminated_directive(caplog):
    candidate, cleaned = BasePlatformAdapter.extract_postgen_candidate_metadata(
        "Made it.\n[[postgen_candidate_id:truncated...\nMEDIA:/tmp/post.png"
    )

    assert candidate is None
    assert "postgen_candidate" not in cleaned
    assert "MEDIA:/tmp/post.png" in cleaned
    assert any("postgen_candidate_directive_invalid" in rec.message for rec in caplog.records)


def test_keyboard_uses_only_the_resolved_registry_row(adapter):
    keyboard = adapter._postgen_candidate_reply_markup(CANDIDATE_ROW)

    rows = keyboard.inline_keyboard
    assert [button.text for button in rows[0]] == ["✅ Approve", "❌ Reject"]
    assert [button.text for button in rows[1]] == ["✏️ Revise"]
    callback_data = [button.callback_data for row in rows for button in row]
    assert callback_data == ["pg:a:gta6q1", "pg:r:gta6q1", "pg:v:gta6q1"]
    # Telegram caps callback payloads at 64 bytes.
    assert all(len(data.encode("utf-8")) <= 64 for data in callback_data)


def test_keyboard_is_none_for_unresolved_rows(adapter):
    assert adapter._postgen_candidate_reply_markup(None) is None
    assert adapter._postgen_candidate_reply_markup({}) is None


def test_candidate_id_extraction_rejects_unusable_ids(adapter, caplog):
    assert adapter._postgen_candidate_id({"postgen_candidate": {"id": "gta6q1"}}) == "gta6q1"
    assert adapter._postgen_candidate_id({"postgen_candidate": {"id": ""}}) is None
    assert adapter._postgen_candidate_id({"postgen_candidate": {"id": "x" * 64}}) is None
    assert adapter._postgen_candidate_id(None) is None
    assert any("postgen_candidate_directive_invalid" in rec.message for rec in caplog.records)


def test_failed_resolution_attaches_no_buttons(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_resolve_postgen_candidate", lambda candidate_id: None)

    metadata = {"postgen_candidate": {"id": "ghost-id"}}
    candidate_id = adapter._postgen_candidate_id(metadata)
    row = adapter._resolve_postgen_candidate(candidate_id) if candidate_id else None
    markup = adapter._postgen_candidate_reply_markup(row)

    assert markup is None


def test_send_image_file_attaches_markup_and_logs_confirmed_delivery(adapter, monkeypatch, tmp_path):
    image_path = tmp_path / "final.png"
    image_path.write_bytes(b"png")

    captured = {}

    async def fake_send_photo(**kwargs):
        captured.update(kwargs)

        class _Msg:
            message_id = 4242

        return _Msg()

    adapter._bot = SimpleNamespace(send_photo=fake_send_photo)
    monkeypatch.setattr(adapter, "_resolve_postgen_candidate", lambda candidate_id: CANDIDATE_ROW)
    deliveries = []
    monkeypatch.setattr(
        adapter,
        "_log_postgen_candidate_delivery",
        lambda row, attached, message_id=None, duration_ms=None: deliveries.append(
            (row, attached, message_id, duration_ms)
        ),
    )
    metadata = {
        "postgen_candidate": {"id": "gta6q1"},
        "notify": True,
    }

    result = asyncio.run(adapter.send_image_file(chat_id="123", image_path=str(image_path), caption="Made it.", metadata=metadata))

    assert result.success
    markup = captured["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        "pg:a:gta6q1",
        "pg:r:gta6q1",
        "pg:v:gta6q1",
    ]
    assert len(deliveries) == 1
    assert deliveries[0][:3] == (CANDIDATE_ROW, True, 4242)
    assert deliveries[0][3] is not None


def test_send_image_file_without_a_resolvable_candidate_sends_plain_photo(adapter, monkeypatch, tmp_path):
    image_path = tmp_path / "final.png"
    image_path.write_bytes(b"png")
    captured = {}

    async def fake_send_photo(**kwargs):
        captured.update(kwargs)

        class _Msg:
            message_id = 7

        return _Msg()

    adapter._bot = SimpleNamespace(send_photo=fake_send_photo)
    monkeypatch.setattr(adapter, "_resolve_postgen_candidate", lambda candidate_id: None)
    deliveries = []
    monkeypatch.setattr(
        adapter,
        "_log_postgen_candidate_delivery",
        lambda row, attached, message_id=None, duration_ms=None: deliveries.append(
            (row, attached, message_id, duration_ms)
        ),
    )
    metadata = {"postgen_candidate": {"id": "stale-id"}}

    result = asyncio.run(adapter.send_image_file(chat_id="123", image_path=str(image_path), caption=None, metadata=metadata))

    assert result.success
    assert "reply_markup" not in captured
    assert len(deliveries) == 1
    assert deliveries[0][:3] == (None, False, 7)
    assert deliveries[0][3] is not None


def test_send_image_file_failure_records_a_measured_delivery_duration(adapter, monkeypatch, tmp_path):
    image_path = tmp_path / "final.png"
    image_path.write_bytes(b"png")

    async def fail_send_photo(**kwargs):
        raise RuntimeError("telegram photo failure")

    async def successful_document_fallback(**kwargs):
        return SimpleNamespace(success=True, message_id="doc-1")

    adapter._bot = SimpleNamespace(send_photo=fail_send_photo)
    monkeypatch.setattr(adapter, "_resolve_postgen_candidate", lambda candidate_id: CANDIDATE_ROW)
    monkeypatch.setattr(adapter, "send_document", successful_document_fallback)
    deliveries = []
    monkeypatch.setattr(
        adapter,
        "_log_postgen_candidate_delivery",
        lambda row, attached, message_id=None, duration_ms=None: deliveries.append(
            (row, attached, message_id, duration_ms)
        ),
    )

    result = asyncio.run(adapter.send_image_file(
        chat_id="123",
        image_path=str(image_path),
        metadata={"postgen_candidate": {"id": "gta6q1"}},
    ))

    assert result.success
    assert deliveries[0][:3] == (CANDIDATE_ROW, False, None)
    assert deliveries[0][3] is not None


def test_resolve_failure_paths_are_logged_as_structured_failures(adapter, monkeypatch, tmp_path):
    logged = []
    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        completed = SimpleNamespace(returncode=1, stdout="", stderr="")
        return completed

    monkeypatch.setenv("POSTGEN_BOT_WORKDIR", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "postgen_candidate_buttons.py").write_text("# stub helper\n")
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.logger",
        SimpleNamespace(warning=lambda *a, **k: logged.append(a)),
    )
    import subprocess as subprocess_module

    monkeypatch.setattr(subprocess_module, "run", fake_run)
    failures = []
    monkeypatch.setattr(adapter, "_log_postgen_candidate_failure", lambda cid: failures.append(cid))

    row = adapter._resolve_postgen_candidate("unknown-id")

    assert row is None
    assert failures == ["unknown-id"]
    assert calls and calls[0][-2:] == ["--id", "unknown-id"]


def test_confirmed_delivery_uses_helper_without_unsupported_log_flags(adapter, monkeypatch, tmp_path):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("POSTGEN_BOT_WORKDIR", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "postgen_candidate_buttons.py").write_text("# helper\n")
    import subprocess as subprocess_module

    monkeypatch.setattr(subprocess_module, "run", fake_run)

    adapter._log_postgen_candidate_delivery(CANDIDATE_ROW, True, 4242, duration_ms=812)

    assert len(commands) == 1
    assert commands[0][2:10] == [
        "delivery", "--id", "gta6q1", "--buttons-attached", "--message-id", "4242", "--duration-ms", "812",
    ]
    assert "--headline-label" not in commands[0]


def test_delivery_timing_flag_is_omitted_when_not_measured(adapter, monkeypatch, tmp_path):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("POSTGEN_BOT_WORKDIR", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "postgen_candidate_buttons.py").write_text("# helper\n")
    import subprocess as subprocess_module

    monkeypatch.setattr(subprocess_module, "run", fake_run)

    adapter._log_postgen_candidate_delivery(CANDIDATE_ROW, False, None)

    assert commands[0][2:] == ["delivery", "--id", "gta6q1", "--no-buttons-attached"]
    assert "--duration-ms" not in commands[0]


def test_end_to_end_helper_marker_to_buttoned_send(adapter, monkeypatch, tmp_path):
    """Helper output → gateway extraction → Telegram send carries the keyboard.

    The helper's real directive command emits ``[[postgen_candidate_id:<id>]]``
    after registering full metadata locally; this captures that exact output
    flowing through extraction into a confirmed buttoned photo send.
    """
    helper_output = "[[postgen_candidate_id:gta6q1]]\n"
    response = f"Made it — highlighted Rockstar.\n{helper_output}MEDIA:/Users/joshua/postgen-bot-work/dist/post.png"

    candidate, cleaned = BasePlatformAdapter.extract_postgen_candidate_metadata(response)
    thread_metadata = dict({"notify": True})
    if candidate:
        thread_metadata["postgen_candidate"] = candidate
    assert "postgen_candidate" not in cleaned
    assert thread_metadata["postgen_candidate"] == {"id": "gta6q1"}

    image_path = tmp_path / "post.png"
    image_path.write_bytes(b"png")
    captured = {}

    async def fake_send_photo(**kwargs):
        captured.update(kwargs)

        class _Msg:
            message_id = 99

        return _Msg()

    adapter._bot = SimpleNamespace(send_photo=fake_send_photo)
    monkeypatch.setattr(adapter, "_resolve_postgen_candidate", lambda candidate_id: {**CANDIDATE_ROW, "id": candidate["id"]})
    monkeypatch.setattr(adapter, "_log_postgen_candidate_delivery", lambda *a, **k: None)

    media_files = [("/Users/joshua/postgen-bot-work/dist/post.png", False)]
    assert len(media_files) == 1
    result = asyncio.run(adapter.send_image_file(chat_id="123", image_path=str(image_path), caption=None, metadata=thread_metadata))

    assert result.success
    markup = captured.get("reply_markup")
    assert markup is not None
    callback_payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callback_payloads == ["pg:a:gta6q1", "pg:r:gta6q1", "pg:v:gta6q1"]


if __name__ == "__main__":
    pytest.main([__file__])
