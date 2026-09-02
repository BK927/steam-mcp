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
        schema_uri = f"steam://schema/steam_search.{mode}"
        for key in ("on_sale", "exclude_owned"):
            if key in filters and not isinstance(filters[key], bool):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"filters.{key} must be boolean.",
                    schema_uri=schema_uri,
                )
        if "tags" in filters:
            tags = filters["tags"]
            if (
                not isinstance(tags, list)
                or len(tags) > 10
                or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
            ):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "filters.tags must contain at most 10 non-empty strings.",
                    schema_uri=schema_uri,
                )
        if "max_price" in filters and mode == "discover":
            value = filters["max_price"]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "filters.max_price must be an integer between 0 and 1000.",
                    schema_uri=schema_uri,
                )
        if "released_within_days" in filters:
            value = filters["released_within_days"]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3_650:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "filters.released_within_days must be an integer between 1 and 3650.",
                    schema_uri=schema_uri,
                )
        enums = {
            "platform": {"win", "mac", "linux"},
            "sort": {"reviews", "release", "price_asc", "price_desc", "relevance"},
            "section": {"top_sellers", "new_releases", "coming_soon", "specials"},
        }
        for key, allowed_values in enums.items():
            if key in filters and filters[key] not in allowed_values:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"filters.{key} is not supported.",
                    schema_uri=schema_uri,
                    details={"allowed": sorted(allowed_values)},
                )
        if "player" in filters and (
            not isinstance(filters["player"], str)
            or not 1 <= len(filters["player"].strip()) <= 200
        ):
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "filters.player must be a non-empty Steam reference.",
                schema_uri=schema_uri,
            )
        language, country = locale_values(locale)
        size = bounded_limit(limit, 10, 30)
        cursor_filters = {
            "mode": mode,
            "query": query,
            "filters": filters,
            "limit": size,
            "language": language,
            "country": country,
        }
        if cursor and mode != "discover":
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"{mode} mode is a bounded snapshot and does not accept cursor.",
                schema_uri=f"steam://schema/steam_search.{mode}",
            )
        state = self.page_state(
            cursor,
            scope=f"search:{mode}",
            filters=cursor_filters,
            initial={"offset": 0},
        )
        page_offset = int(state.get("offset", 0))
        next_value: str | None = None
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
                    "offset": page_offset,
                    "country_code": country,
                },
                ttl=300,
            )
            preferred = ("results", "games")
            if isinstance(data, dict) and data.get("has_more"):
                next_offset = data.get("next_offset")
                if isinstance(next_offset, int) and next_offset > page_offset:
                    next_value = self.next_cursor(
                        scope=f"search:{mode}",
                        filters=cursor_filters,
                        state={"offset": next_offset},
                    )
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
            if isinstance(data, dict):
                data = {
                    **data,
                    "result_scope": "top_n_snapshot",
                    "requested_limit": size,
                    "pagination_supported": False,
                }
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
            if isinstance(data, dict):
                data = {
                    **data,
                    "result_scope": "top_n_snapshot",
                    "requested_limit": size,
                    "pagination_supported": False,
                }
            preferred = ("games", "items", "results")
        return self.result_envelope(
            data,
            canonical_uri="steam://entity/search/current",
            provider="steam_store",
            preferred_items=preferred,
            next_cursor=next_value,
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
