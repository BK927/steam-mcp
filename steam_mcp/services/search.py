"""Steam title lookup, discovery, deals, and storefront charts."""

from __future__ import annotations

from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, bounded_limit, locale_values

SEARCH_MODES = frozenset({"lookup", "discover", "deals", "chart"})


class SearchService(BaseService):
    async def search(
        self,
        mode: str,
        query: str,
        filters: dict[str, Any],
        cursor: str,
        limit: int,
        locale: dict[str, Any],
    ) -> dict[str, Any]:
        del cursor  # current upstream searches expose one bounded page
        if mode not in SEARCH_MODES:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported search mode: {mode}.",
                schema_uri=f"steam://schema/steam_search.{mode}",
            )
        language, country = locale_values(locale)
        size = bounded_limit(limit, 10, 30)
        if mode == "lookup":
            if not query.strip():
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "lookup mode requires query.")
            data = await self.call(
                "steam_search_apps",
                {"query": query, "limit": size, "country_code": country, "language": language},
                ttl=300,
            )
            preferred = ("results",)
        elif mode == "discover":
            data = await self.call(
                "steam_discover",
                {
                    "term": query or None,
                    "tags": filters.get("tags", []),
                    "max_price": filters.get("max_price"),
                    "on_sale": bool(filters.get("on_sale", False)),
                    "platform": filters.get("platform"),
                    "sort": filters.get("sort", "reviews"),
                    "steamid": filters.get("player"),
                    "exclude_owned": bool(filters.get("exclude_owned", True)),
                    "released_within_days": filters.get("released_within_days"),
                    "limit": size,
                    "country_code": country,
                },
                ttl=300,
            )
            preferred = ("results", "games")
        elif mode == "deals":
            data = await self.call(
                "steam_get_featured_specials", {"limit": size, "country_code": country}, ttl=120
            )
            preferred = ("specials", "games", "items")
        else:
            data = await self.call(
                "steam_get_store_highlights",
                {
                    "section": filters.get("section", "top_sellers"),
                    "limit": size,
                    "country_code": country,
                },
                ttl=120,
            )
            preferred = ("games", "items", "results")
        return self.result_envelope(
            data,
            canonical_uri="steam://entity/search/current",
            provider="steam_store",
            preferred_items=preferred,
        )
