"""TinyFish web search + content extraction — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Two
capabilities advertised:

- ``supports_search()``  -> True (TinyFish search API)
- ``supports_extract()`` -> True (TinyFish fetch API)

Both are sync — the underlying call is ``httpx`` (no new dependency; the
sibling Tavily provider uses the same client). The web_extract_tool
dispatcher wraps sync extracts via ``asyncio.to_thread`` when it needs to
keep the event loop responsive.

Config keys this provider responds to::

    web:
      search_backend: "tinyfish"     # explicit per-capability
      extract_backend: "tinyfish"    # explicit per-capability
      backend: "tinyfish"            # shared fallback for both

Env var::

    TINYFISH_API_KEY=...   # free key at https://agent.tinyfish.ai/api-keys

Auth is header-based (``X-API-Key``). TinyFish has no anonymous/keyless
tier, so :meth:`is_keyless_available` stays False.

API (docs.tinyfish.ai):

- Search:  ``GET  https://api.search.tinyfish.ai``  params: ``query`` (req),
  optional ``location``/``language``/``recency_minutes``/``after_date``/
  ``before_date``/``domain_type``/``purpose``. Response::

      {"query", "results": [{"position", "site_name", "title",
                             "snippet", "url"}], "total_results", "page"}

- Fetch:   ``POST https://api.fetch.tinyfish.ai``  JSON body: ``{urls: [<=10],
  format: markdown|html|json, ttl?, include_selectors?, exclude_selectors?,
  purpose?}``. Response::

      {"results": [{"url", "final_url", "title", "description",
                    "language", "format", "text"}],
       "errors":  [{"url", "error"}]}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.search.tinyfish.ai"
_FETCH_URL = "https://api.fetch.tinyfish.ai"

# Fetch accepts at most 10 URLs per request (docs.tinyfish.ai).
_FETCH_BATCH = 10
_MAX_IMAGE_LINKS = 200

_VALID_FORMATS = {"markdown", "html", "json"}


def _tinyfish_headers(api_key: str) -> Dict[str, str]:
    """Build TinyFish request headers (X-API-Key auth)."""
    return {"X-API-Key": api_key}


class TinyFishWebSearchProvider(WebSearchProvider):
    """TinyFish search + extract provider (free tier, key required)."""

    @property
    def name(self) -> str:
        return "tinyfish"

    @property
    def display_name(self) -> str:
        return "TinyFish (Free)"

    def is_available(self) -> bool:
        """Return True when ``TINYFISH_API_KEY`` is set to a non-empty value."""
        from agent.web_search_provider import get_provider_env

        return bool(get_provider_env("TINYFISH_API_KEY"))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    # ------------------------------------------------------------------ search

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a TinyFish search.

        Returns ``{"success": True, "data": {"web": [{...}, ...]}}`` on
        success, ``{"success": False, "error": str}`` on failure (incl.
        missing API key and HTTP errors).
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            from agent.web_search_provider import get_provider_env

            api_key = get_provider_env("TINYFISH_API_KEY")
            if not api_key:
                return {
                    "success": False,
                    "error": (
                        "TINYFISH_API_KEY environment variable not set. "
                        "Get a free key at https://agent.tinyfish.ai/api-keys"
                    ),
                }

            logger.info("TinyFish search: '%s' (limit=%d)", query, limit)
            response = httpx.get(
                _SEARCH_URL,
                params={"query": query},
                headers=_tinyfish_headers(api_key),
                timeout=60,
            )
            if response.status_code >= 400:
                body = (response.text or "").strip()
                detail = body or f"HTTP {response.status_code}"
                return {"success": False, "error": f"TinyFish search failed: {detail}"}

            raw = response.json()
            web_results: List[Dict[str, Any]] = []
            for i, result in enumerate(raw.get("results", []) or []):
                if i >= limit:
                    break
                web_results.append(
                    {
                        "title": result.get("title", "") or "",
                        "url": result.get("url", "") or "",
                        "description": result.get("snippet", "") or "",
                        "position": result.get("position", i + 1),
                    }
                )
            return {"success": True, "data": {"web": web_results}}
        except Exception as exc:  # noqa: BLE001 — including httpx errors
            logger.warning("TinyFish search error: %s", exc)
            return {"success": False, "error": f"TinyFish search failed: {exc}"}

    # ----------------------------------------------------------------- extract

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via TinyFish's fetch API.

        Sync — the underlying call is ``httpx.post(...)``. URLs are chunked
        into batches of ten (the API's per-request cap). Returns the legacy
        list-of-results shape; per-URL failures become items with ``error``.

        ``kwargs`` may carry a ``format`` of markdown|html|json (default
        markdown); unknown keys are ignored per the ABC contract.
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": u, "error": "Interrupted", "title": ""} for u in urls
                ]

            from agent.web_search_provider import get_provider_env

            api_key = get_provider_env("TINYFISH_API_KEY")
            if not api_key:
                err = (
                    "TINYFISH_API_KEY environment variable not set. "
                    "Get a free key at https://agent.tinyfish.ai/api-keys"
                )
                return [
                    {"url": u, "title": "", "content": "", "error": err} for u in urls
                ]

            fmt = str(kwargs.get("format") or "markdown").lower().strip()
            if fmt not in _VALID_FORMATS:
                fmt = "markdown"

            logger.info(
                "TinyFish extract: %d URL(s) (format=%s)", len(urls), fmt
            )

            documents: List[Dict[str, Any]] = []
            for start in range(0, len(urls), _FETCH_BATCH):
                batch = list(urls[start : start + _FETCH_BATCH])
                response = httpx.post(
                    _FETCH_URL,
                    json={"urls": batch, "format": fmt, "image_links": True},
                    headers=_tinyfish_headers(api_key),
                    timeout=120,
                )
                if response.status_code >= 400:
                    body = (response.text or "").strip()
                    detail = body or f"HTTP {response.status_code}"
                    for u in batch:
                        documents.append(
                            {
                                "url": u,
                                "title": "",
                                "content": "",
                                "raw_content": "",
                                "error": f"TinyFish extract failed: {detail}",
                                "metadata": {"sourceURL": u},
                            }
                        )
                    continue

                raw = response.json()
                for result in raw.get("results", []) or []:
                    url = result.get("url", "") or result.get("final_url", "")
                    text = result.get("text", "") or ""
                    if not isinstance(text, str):
                        text = json.dumps(text, ensure_ascii=False)
                    title = result.get("title", "") or ""
                    image_links = [
                        u for u in (result.get("image_links") or []) if isinstance(u, str) and u
                    ]
                    if image_links:
                        # The web_extract tool forwards only url/title/content to the
                        # model, so the page's <img src> URLs ride along inside content.
                        text = text.rstrip() + "\n\n## Images on this page\n" + "\n".join(
                            f"- {u}" for u in image_links[:_MAX_IMAGE_LINKS]
                        )
                    documents.append(
                        {
                            "url": url,
                            "title": title,
                            "content": text,
                            "raw_content": text,
                            "metadata": {
                                "sourceURL": url,
                                "finalURL": result.get("final_url", "") or url,
                                "title": title,
                                "description": result.get("description", "") or "",
                                "language": result.get("language", "") or "",
                                "format": result.get("format", fmt) or fmt,
                            },
                        }
                    )
                for fail in raw.get("errors", []) or []:
                    url = fail.get("url", "") or ""
                    documents.append(
                        {
                            "url": url,
                            "title": "",
                            "content": "",
                            "raw_content": "",
                            "error": fail.get("error", "extraction failed"),
                            "metadata": {"sourceURL": url},
                        }
                    )
            return documents
        except Exception as exc:  # noqa: BLE001 — including httpx errors
            logger.warning("TinyFish extract error: %s", exc)
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "error": f"TinyFish extract failed: {exc}",
                }
                for u in urls
            ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "TinyFish (Free)",
            "badge": "free",
            "tag": (
                "Free web search + content extraction. Set TINYFISH_API_KEY "
                "(free key at https://agent.tinyfish.ai/api-keys)."
            ),
            "env_vars": [
                {
                    "key": "TINYFISH_API_KEY",
                    "prompt": "TinyFish API key",
                    "url": "https://agent.tinyfish.ai/api-keys",
                },
            ],
        }
