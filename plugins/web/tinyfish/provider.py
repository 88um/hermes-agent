"""TinyFish web search + content extraction (``/search``, ``/fetch``; sync httpx).

Env: ``TINYFISH_API_KEY`` (free key at https://agent.tinyfish.ai/api-keys), sent as
``X-API-Key``. TinyFish has no keyless tier. Select it with ``web.backend: tinyfish``
(or ``web.search_backend`` / ``web.extract_backend``).

API (docs.tinyfish.ai):

- Search: ``GET https://api.search.tinyfish.ai`` with ``query``; results carry
  ``position``, ``title``, ``snippet`` and ``url``.
- Fetch: ``POST https://api.fetch.tinyfish.ai`` with ``{urls: [<=10], format:
  markdown|html|json, image_links}``; returns ``results`` (``url``, ``final_url``,
  ``title``, ``description``, ``language``, ``format``, ``text``, ``image_links``)
  and per-URL ``errors``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

from plugins.web._common import (
    BaseWebSearchProvider, extract_fail, http_status_detail, provider_env, run_extract, run_search, search_fail,
    search_ok, setup_schema, title_hit,
)

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.search.tinyfish.ai"
_FETCH_URL = "https://api.fetch.tinyfish.ai"

# Fetch accepts at most 10 URLs per request (docs.tinyfish.ai).
_FETCH_BATCH = 10
_MAX_IMAGE_LINKS = 200

_VALID_FORMATS = {"markdown", "html", "json"}

_MISSING_KEY = "TINYFISH_API_KEY environment variable not set. Get a free key at https://agent.tinyfish.ai/api-keys"


def _headers(api_key: str) -> Dict[str, str]:
    return {"X-API-Key": api_key}


def _search_rows(raw: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    return [
        title_hit(r.get("title", "") or "", r.get("url", "") or "", r.get("snippet", "") or "", r.get("position", i + 1))
        for i, r in enumerate((raw.get("results") or [])[:limit])
    ]


def _failed_document(url: str, error: str) -> Dict[str, Any]:
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error, "metadata": {"sourceURL": url}}


def _fetched_document(result: Dict[str, Any], fmt: str) -> Dict[str, Any]:
    url = result.get("url", "") or result.get("final_url", "")
    text = result.get("text", "") or ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    title = result.get("title", "") or ""
    image_links = [u for u in (result.get("image_links") or []) if isinstance(u, str) and u]
    if image_links:
        # web_extract forwards only url/title/content to the model, so the page's
        # <img src> URLs ride along inside content.
        text = text.rstrip() + "\n\n## Images on this page\n" + "\n".join(
            f"- {u}" for u in image_links[:_MAX_IMAGE_LINKS]
        )
    return {
        "url": url, "title": title, "content": text, "raw_content": text,
        "metadata": {
            "sourceURL": url,
            "finalURL": result.get("final_url", "") or url,
            "title": title,
            "description": result.get("description", "") or "",
            "language": result.get("language", "") or "",
            "format": result.get("format", fmt) or fmt,
        },
    }


class TinyFishWebSearchProvider(BaseWebSearchProvider):
    """TinyFish search + extract provider (free tier, key required)."""

    NAME = "tinyfish"
    DISPLAY_NAME = "TinyFish (Free)"
    KEY_ENV = "TINYFISH_API_KEY"
    EXTRACT = True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        def _body() -> Dict[str, Any]:
            api_key = provider_env(self.KEY_ENV)
            if not api_key:
                return search_fail(_MISSING_KEY)
            logger.info("TinyFish search: '%s' (limit=%d)", query, limit)
            response = httpx.get(_SEARCH_URL, params={"query": query}, headers=_headers(api_key), timeout=60)
            if response.status_code >= 400:
                return search_fail(f"TinyFish search failed: {http_status_detail(response)}")
            return search_ok(_search_rows(response.json(), limit))

        # Not verbatim: a malformed body's JSONDecodeError is a ValueError too, and must keep the prefix.
        return run_search("TinyFish", logger, _body, verbatim_value_error=False)

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch ``urls`` in batches of ten. ``format`` may be markdown|html|json (default
        markdown); a batch the API refuses becomes one error entry per URL."""
        def _body() -> List[Dict[str, Any]]:
            api_key = provider_env(self.KEY_ENV)
            if not api_key:
                return extract_fail(urls, _MISSING_KEY)
            fmt = str(kwargs.get("format") or "markdown").lower().strip()
            if fmt not in _VALID_FORMATS:
                fmt = "markdown"
            logger.info("TinyFish extract: %d URL(s) (format=%s)", len(urls), fmt)
            documents: List[Dict[str, Any]] = []
            for start in range(0, len(urls), _FETCH_BATCH):
                batch = list(urls[start:start + _FETCH_BATCH])
                response = httpx.post(
                    _FETCH_URL, json={"urls": batch, "format": fmt, "image_links": True},
                    headers=_headers(api_key), timeout=120,
                )
                if response.status_code >= 400:
                    error = f"TinyFish extract failed: {http_status_detail(response)}"
                    documents += [_failed_document(u, error) for u in batch]
                    continue
                raw = response.json()
                documents += [_fetched_document(r, fmt) for r in raw.get("results") or []]
                documents += [
                    _failed_document(f.get("url", "") or "", f.get("error", "extraction failed"))
                    for f in raw.get("errors") or []
                ]
            return documents

        return run_extract("TinyFish", logger, urls, _body, verbatim_value_error=False)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "TinyFish (Free)", "free",
            "Free web search + content extraction. Set TINYFISH_API_KEY (free key at https://agent.tinyfish.ai/api-keys).",
            "TINYFISH_API_KEY", "TinyFish API key", "https://agent.tinyfish.ai/api-keys",
        )
