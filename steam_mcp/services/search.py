"""Steam title lookup, discovery, deals, and storefront charts."""

from __future__ import annotations

import math
from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, bounded_limit, locale_values

SEARCH_MODES = frozenset({"lookup", "discover", "deals", "chart"})
SEARCH_FILTERS = {
    "lookup": frozenset(),
    "discover": frozenset(
        {
            "tags",
            "max_price",
            "on_sale",
            "platform",
            "sort",
            "player",
            "exclude_owned",
            "released_within_days",
        }
    ),
    "deals": frozenset({"max_price", "min_discount"}),
    "chart": frozenset({"section"}),
}


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
        unknown = sorted(set(filters) - SEARCH_FILTERS[mode])
        if unknown:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported filters for {mode}: {', '.join(unknown)}.",
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
            max_price = self._optional_number(filters, "max_price", minimum=0)
            min_discount = self._optional_number(
                filters, "min_discount", minimum=0, maximum=100
            )
            data = await self.call(
                "steam_get_featured_specials",
                {
                    "limit": 50 if filters else size,
                    "country_code": country,
                },
                ttl=120,
            )
            if isinstance(data, dict) and isinstance(data.get("specials"), list):
                rows = data["specials"]
                if max_price is not None:
                    rows = [
                        row
                        for row in rows
                        if self._row_number(row, "final_price") is not None
                        and self._row_number(row, "final_price") <= max_price
                    ]
                if min_discount is not None:
                    rows = [
                        row
                        for row in rows
                        if (self._row_number(row, "discount_pct") or 0) >= min_discount
                    ]
                data = {**data, "specials": rows[:size], "count": len(rows[:size])}
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

    @staticmethod
    def _optional_number(
        filters: dict[str, Any],
        name: str,
        *,
        minimum: float,
        maximum: float | None = None,
    ) -> float | None:
        value = filters.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"filters.{name} must be numeric.")
        number = float(value)
        if not math.isfinite(number):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"filters.{name} must be finite.")
        if number < minimum or (maximum is not None and number > maximum):
            bound = f" between {minimum:g} and {maximum:g}" if maximum is not None else f" at least {minimum:g}"
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"filters.{name} must be{bound}.",
            )
        return number

    @staticmethod
    def _row_number(row: dict[str, Any], name: str) -> float | None:
        try:
            number = float(row.get(name))
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
