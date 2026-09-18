"""TinyFish web search + extract plugin — bundled, auto-loaded.

Mirrors the ``plugins/web/tavily/`` layout: ``provider.py`` holds the
provider class, ``__init__.py::register(ctx)`` registers an instance.
"""

from __future__ import annotations

from plugins.web.tinyfish.provider import TinyFishWebSearchProvider


def register(ctx) -> None:
    """Register the TinyFish provider with the plugin context."""
    ctx.register_web_search_provider(TinyFishWebSearchProvider())
