"""Bounded Steam review summaries and signed-cursor pages."""

from __future__ import annotations

from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, bounded_limit, locale_values


class ReviewsService(BaseService):
    async def get(
        self,
        game: str | int,
        mode: str,
        filters: dict[str, Any],
        cursor: str,
        limit: int,
        max_text_chars_per_item: int,
        locale: dict[str, Any],
    ) -> dict[str, Any]:
        if mode not in {"summary", "page"}:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported review mode: {mode}.",
                schema_uri=f"steam://schema/steam_reviews_get.{mode}",
            )
        language, country = locale_values(locale)
        appid = await self.appid(game, country, language)
        size = bounded_limit(limit, 20, 100)
        text_limit = max(0, min(max_text_chars_per_item or 1_200, 4_000))
        canonical = f"steam://entity/app/{appid}"
        if mode == "summary":
            data = await self.call(
                "steam_get_app_reviews",
                {
                    "appid": appid,
                    "review_filter": filters.get("review_filter", "all"),
                    "day_range": int(filters.get("day_range", 30)),
                    "recent_max_reviews": int(filters.get("recent_max_reviews", 600)),
                    "review_type": filters.get("review_type", "all"),
                    "purchase_type": filters.get("purchase_type", "all"),
                    "limit": min(size, 20),
                    "country_code": country,
                    "language": language,
                },
                ttl=300,
            )
            return self.result_envelope(
                data,
                canonical_uri=canonical,
                provider="steam_reviews",
                preferred_items=("reviews",),
                untrusted_fields=["items[].excerpt", "items[].review"],
            )

        cursor_filters = {
            "appid": appid,
            "filters": filters,
            "limit": size,
            "text_limit": text_limit,
            "language": language,
            "country": country,
        }
        state = self.page_state(
            cursor,
            scope="reviews:page",
            filters=cursor_filters,
            initial={"upstream_cursor": "*"},
        )
        upstream = str(state.get("upstream_cursor") or "*")
        data = await self.call(
            "steam_get_app_review_batch",
            {
                "appid": appid,
                "cursor": upstream,
                "sort_by": filters.get("sort_by", "recent"),
                "page_size": size,
                "review_type": filters.get("review_type", "all"),
                "purchase_type": filters.get("purchase_type", "all"),
                "language": language,
                "include_offtopic_activity": bool(filters.get("include_offtopic_activity", False)),
                "max_text_chars": text_limit,
                "include_author_id": bool(filters.get("include_author_id", False)),
                "country_code": country,
            },
            ttl=60,
        )
        page = data.get("page", {}) if isinstance(data, dict) else {}
        next_upstream = page.get("next_cursor")
        next_value = self.next_cursor(
            scope="reviews:page",
            filters=cursor_filters,
            state={"upstream_cursor": next_upstream} if page.get("has_more") and next_upstream else None,
        )
        return self.result_envelope(
            data,
            canonical_uri=canonical,
            provider="steam_reviews",
            preferred_items=("reviews",),
            next_cursor=next_value,
            untrusted_fields=["items[].review", "items[].developer_response"],
        )
