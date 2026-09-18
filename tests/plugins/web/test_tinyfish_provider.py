"""Unit tests for the TinyFish web search/extract provider.

HTTP is mocked at the ``httpx`` boundary (the same client the provider and
its sibling Tavily provider use). Covers:

- search response → ``{success, data: {web: [...]}}`` mapping
- extract response → legacy document list mapping (incl. per-URL errors)
- extract URL chunking in batches of ten
- missing TINYFISH_API_KEY → unavailable + typed error (no HTTP call)
- HTTP error → clear failure message rather than a raise
"""
from __future__ import annotations

import pytest

from plugins.web.tinyfish.provider import TinyFishWebSearchProvider


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)


def test_is_available_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TinyFishWebSearchProvider()
    assert p.is_available() is False
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    assert p.is_available() is True


def test_capabilities_and_name() -> None:
    p = TinyFishWebSearchProvider()
    assert p.name == "tinyfish"
    assert p.display_name == "TinyFish (Free)"
    assert p.supports_search() is True
    assert p.supports_extract() is True
    assert p.is_keyless_available() is False


def test_search_maps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    payload = {
        "query": "cats",
        "results": [
            {"position": 1, "site_name": "Ex", "title": "T1", "snippet": "S1", "url": "https://a"},
            {"position": 2, "site_name": "Ex", "title": "T2", "snippet": "S2", "url": "https://b"},
        ],
        "total_results": 2,
        "page": 1,
    }
    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResponse(200, payload)

    monkeypatch.setattr("plugins.web.tinyfish.provider.httpx.get", _fake_get)

    out = TinyFishWebSearchProvider().search("cats", limit=5)
    assert out["success"] is True
    web = out["data"]["web"]
    assert web == [
        {"title": "T1", "url": "https://a", "description": "S1", "position": 1},
        {"title": "T2", "url": "https://b", "description": "S2", "position": 2},
    ]
    assert captured["headers"] == {"X-API-Key": "k"}
    assert captured["params"] == {"query": "cats"}


def test_search_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    payload = {"results": [{"title": f"T{i}", "url": f"u{i}", "snippet": "s"} for i in range(10)]}
    monkeypatch.setattr(
        "plugins.web.tinyfish.provider.httpx.get",
        lambda *a, **k: _FakeResponse(200, payload),
    )
    out = TinyFishWebSearchProvider().search("q", limit=3)
    assert len(out["data"]["web"]) == 3


def test_search_missing_key_no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("HTTP should not be called without a key")

    monkeypatch.setattr("plugins.web.tinyfish.provider.httpx.get", _boom)
    out = TinyFishWebSearchProvider().search("q")
    assert out["success"] is False
    assert "TINYFISH_API_KEY" in out["error"]


def test_search_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    monkeypatch.setattr(
        "plugins.web.tinyfish.provider.httpx.get",
        lambda *a, **k: _FakeResponse(429, text="rate limited"),
    )
    out = TinyFishWebSearchProvider().search("q")
    assert out["success"] is False
    assert "rate limited" in out["error"]


def test_extract_maps_documents_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    payload = {
        "results": [
            {
                "url": "https://a",
                "final_url": "https://a/final",
                "title": "TA",
                "description": "DA",
                "language": "en",
                "format": "markdown",
                "text": "body A",
            }
        ],
        "errors": [{"url": "https://b", "error": "timeout"}],
    }
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, payload)

    monkeypatch.setattr("plugins.web.tinyfish.provider.httpx.post", _fake_post)

    docs = TinyFishWebSearchProvider().extract(["https://a", "https://b"])
    assert captured["url"] == "https://api.fetch.tinyfish.ai"
    assert captured["json"]["format"] == "markdown"
    ok = [d for d in docs if "error" not in d]
    bad = [d for d in docs if "error" in d]
    assert len(ok) == 1 and len(bad) == 1
    assert ok[0]["url"] == "https://a"
    assert ok[0]["content"] == "body A"
    assert ok[0]["raw_content"] == "body A"
    assert ok[0]["metadata"]["finalURL"] == "https://a/final"
    assert bad[0]["url"] == "https://b"
    assert bad[0]["error"] == "timeout"


def test_extract_honours_format_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    seen = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        seen["format"] = json["format"]
        return _FakeResponse(200, {"results": [], "errors": []})

    monkeypatch.setattr("plugins.web.tinyfish.provider.httpx.post", _fake_post)
    TinyFishWebSearchProvider().extract(["https://a"], format="html")
    assert seen["format"] == "html"
    # unknown format falls back to markdown
    TinyFishWebSearchProvider().extract(["https://a"], format="pdf")
    assert seen["format"] == "markdown"


def test_extract_chunks_in_batches_of_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    batches = []

    def _fake_post(url, json=None, headers=None, timeout=None):
        batches.append(len(json["urls"]))
        return _FakeResponse(200, {"results": [], "errors": []})

    monkeypatch.setattr("plugins.web.tinyfish.provider.httpx.post", _fake_post)
    urls = [f"https://x/{i}" for i in range(23)]
    TinyFishWebSearchProvider().extract(urls)
    assert batches == [10, 10, 3]


def test_extract_missing_key_no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("HTTP should not be called without a key")

    monkeypatch.setattr("plugins.web.tinyfish.provider.httpx.post", _boom)
    docs = TinyFishWebSearchProvider().extract(["https://a"])
    assert len(docs) == 1
    assert "TINYFISH_API_KEY" in docs[0]["error"]


def test_extract_http_error_marks_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    monkeypatch.setattr(
        "plugins.web.tinyfish.provider.httpx.post",
        lambda *a, **k: _FakeResponse(500, text="boom"),
    )
    docs = TinyFishWebSearchProvider().extract(["https://a", "https://b"])
    assert len(docs) == 2
    assert all("boom" in d["error"] for d in docs)


def test_extract_requests_image_links_and_appends_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    payload = {
        "results": [
            {
                "url": "https://a",
                "title": "TA",
                "text": "body A",
                "image_links": ["https://a/hero.jpg", "", "https://a/2.png"],
            }
        ],
        "errors": [],
    }
    seen = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        seen["json"] = json
        return _FakeResponse(200, payload)

    monkeypatch.setattr("plugins.web.tinyfish.provider.httpx.post", _fake_post)
    docs = TinyFishWebSearchProvider().extract(["https://a"])
    assert seen["json"]["image_links"] is True
    assert docs[0]["content"].startswith("body A")
    assert "## Images on this page" in docs[0]["content"]
    assert "- https://a/hero.jpg" in docs[0]["content"]
    assert "- https://a/2.png" in docs[0]["content"]
    assert "- \n" not in docs[0]["content"]
