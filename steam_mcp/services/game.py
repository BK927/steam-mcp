"""Known-game reads translated to the existing Steam providers."""

from __future__ import annotations

from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, bounded_limit, locale_values

GAME_VIEWS = frozenset(
    {"summary", "store", "compatibility", "technical", "dlc", "tags", "achievements", "live", "news", "pricing"}
)


class GameService(BaseService):
    async def get(
        self,
        game: str | int,
        view: str,
        select: list[str],
        options: dict[str, Any],
        cursor: str,
        limit: int,
        locale: dict[str, Any],
    ) -> dict[str, Any]:
        if view not in GAME_VIEWS:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported game view: {view}.",
                schema_uri=f"steam://schema/steam_game_get.{view}",
            )
        language, country = locale_values(locale)
        appid = await self.appid(game, country, language)
        size = bounded_limit(limit, 20, 100)
        filters = {
            "appid": appid,
            "view": view,
            "select": sorted(select),
            "options": options,
            "limit": size,
            "language": language,
            "country": country,
        }
        state = self.page_state(cursor, scope=f"game:{view}", filters=filters, initial={"offset": 0})
        offset = int(state.get("offset", 0))
        preferred: tuple[str, ...] = ()
        has_more = False

        if view in {"summary", "store"}:
            data = await self.call(
                "steam_get_app_details",
                {
                    "appid": appid,
                    "country_code": country,
                    "language": language,
                    "include_requirements": bool(options.get("include_requirements", view == "store")),
                    "include_long_description": bool(options.get("include_long_description", False)),
                },
                ttl=600,
            )
        elif view == "compatibility":
            data = await self.call(
                "steam_get_deck_compatibility", {"appid": appid, "language": language}, ttl=3_600
            )
        elif view == "technical":
            sections = select or [str(options.get("section") or "product")]
            allowed = {"product", "branches", "depots", "current_build"}
            if set(sections) - allowed:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "Unknown technical section.")
            data = {}
            for section in sections:
                if section == "product":
                    data[section] = await self.call(
                        "steam_get_product_info",
                        {
                            "appid": appid,
                            "branch": options.get("branch", "public"),
                            "include_launch_options": bool(options.get("include_launch_options", False)),
                        },
                    )
                elif section == "branches":
                    data[section] = await self.call(
                        "steam_get_branches", {"appid": appid, "limit": size, "offset": offset}
                    )
                elif section == "depots":
                    data[section] = await self.call(
                        "steam_get_depots",
                        {
                            "appid": appid,
                            "branch": options.get("branch", "public"),
                            "platform": options.get("platform", "all"),
                            "include_all_manifests": bool(options.get("include_all_manifests", False)),
                            "limit": size,
                            "offset": offset,
                        },
                    )
                else:
                    data[section] = await self.call(
                        "steam_get_current_build",
                        {
                            "appid": appid,
                            "branch": options.get("branch", "public"),
                            "platform": options.get("platform", "all"),
                            "limit": size,
                            "offset": offset,
                        },
                    )
            has_more = any(
                isinstance(value, dict)
                and bool(value.get("has_more") or (value.get("page") or {}).get("has_more"))
                for value in data.values()
            )
        elif view == "dlc":
            data = await self.call(
                "steam_get_dlc",
                {
                    "appid": appid,
                    "limit": size,
                    "enrich": bool(options.get("enrich", True)),
                    "on_sale_only": bool(options.get("on_sale_only", False)),
                    "country_code": country,
                },
            )
            preferred = ("dlc", "items")
            has_more = bool(isinstance(data, dict) and data.get("has_more"))
        elif view == "tags":
            data = await self.call(
                "steam_get_app_tags", {"appid": appid, "limit": size, "country_code": country}
            )
            preferred = ("tags",)
        elif view == "achievements":
            sections = select or ["definitions", "global_rates"]
            data = {}
            if "definitions" in sections:
                data["definitions"] = await self.call("steam_get_game_schema", {"appid": appid})
            if "global_rates" in sections:
                data["global_rates"] = await self.call(
                    "steam_get_global_achievement_percentages", {"appid": appid}
                )
            if not data:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "Select definitions and/or global_rates.")
        elif view == "live":
            data = await self.call("steam_get_current_players", {"appid": appid}, ttl=60)
        elif view == "news":
            data = await self.call("steam_get_app_news", {"appid": appid, "count": size}, ttl=300)
            preferred = ("news", "items")
        else:
            data = await self.call(
                "steam_get_app_regional_pricing",
                {"appid": appid, "countries": options.get("countries") or [country]},
                ttl=300,
            )
            preferred = ("prices", "regions")

        next_value = self.next_cursor(
            scope=f"game:{view}",
            filters=filters,
            state={"offset": offset + size} if has_more else None,
        )
        return self.result_envelope(
            data,
            canonical_uri=f"steam://entity/app/{appid}",
            provider="steamcmd" if view == "technical" else "steam_store",
            preferred_items=preferred,
            next_cursor=next_value,
        )
