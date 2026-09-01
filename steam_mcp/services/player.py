"""Player profile, social, library, progress, and inventory reads."""

from __future__ import annotations

from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, bounded_limit, locale_values

PLAYER_VIEWS = frozenset({"profile", "social", "library", "wishlist", "progress", "inventory"})


class PlayerService(BaseService):
    async def get(
        self,
        player: str | list[str] | None,
        view: str,
        game: str | int | None,
        select: list[str],
        options: dict[str, Any],
        cursor: str,
        limit: int,
        locale: dict[str, Any],
    ) -> dict[str, Any]:
        if view not in PLAYER_VIEWS:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported player view: {view}.",
                schema_uri=f"steam://schema/steam_player_get.{view}",
            )
        players: list[str] | None = None
        if isinstance(player, list):
            players = [str(value).strip() for value in player]
            if not 1 <= len(players) <= 100 or any(not value for value in players):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "player array must contain between 1 and 100 non-empty references.",
                )
            if view != "profile":
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "player arrays are supported only for the profile view.",
                    schema_uri="steam://schema/steam_player_get.profile",
                )
            if select and any(section != "summary" for section in select):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "multi-player profile requests support only select=['summary'].",
                    schema_uri="steam://schema/steam_player_get.profile",
                )
        elif player is not None and not isinstance(player, str):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "player must be a string or string array.")
        elif isinstance(player, str) and not player.strip():
            player = None
        elif isinstance(player, str):
            player = player.strip()
        language, country = locale_values(locale)
        size = bounded_limit(limit, 25, 100)
        filters = {
            "player": players if players is not None else player,
            "view": view,
            "game": str(game or ""),
            "select": sorted(select),
            "options": options,
            "limit": size,
            "language": language,
            "country": country,
        }
        state = self.page_state(cursor, scope=f"player:{view}", filters=filters, initial={"offset": 0})
        offset = int(state.get("offset", 0))
        target = player if isinstance(player, str) else None
        preferred: tuple[str, ...] = ()
        has_more = False

        if view == "profile":
            sections = select or (["summary"] if players is not None else ["summary", "level", "bans", "badges"])
            data: dict[str, Any] = {}
            if "steam_id" in sections or "summary" in sections:
                data["summary"] = await self.call(
                    "steam_get_player_summary",
                    {"steamids": players if players is not None else ([target] if target else [])},
                    ttl=120,
                )
            if "level" in sections:
                data["level"] = await self.call("steam_get_steam_level", {"steamid": target}, ttl=300)
            if "bans" in sections:
                data["bans"] = await self.call("steam_get_player_bans", {"steamid": target}, ttl=300)
            if "badges" in sections:
                data["badges"] = await self.call("steam_get_player_badges", {"steamid": target}, ttl=300)
        elif view == "social":
            sections = select or ["friends", "groups"]
            data = {}
            if "friends" in sections:
                data["friends"] = await self.call(
                    "steam_get_friend_list",
                    {
                        "steamid": target,
                        "limit": size,
                        "offset": offset,
                        "online_only": bool(options.get("online_only", False)),
                    },
                    ttl=120,
                )
            if "groups" in sections:
                data["groups"] = await self.call(
                    "steam_get_user_groups",
                    {
                        "steamid": target,
                        "limit": size,
                        "enrich": bool(options.get("enrich", True)),
                    },
                    ttl=300,
                )
            has_more = any(
                isinstance(value, dict)
                and bool(value.get("has_more") or (value.get("page") or {}).get("has_more"))
                for value in data.values()
            )
        elif view == "library":
            scope = str(options.get("scope") or "owned")
            if scope == "recent":
                data = await self.call("steam_get_recently_played_games", {"steamid": target}, ttl=120)
            elif scope == "owned":
                data = await self.call(
                    "steam_get_owned_games",
                    {
                        "steamid": target,
                        "limit": size,
                        "offset": offset,
                        "sort_by": options.get("sort_by", "playtime"),
                        "include_free_games": bool(options.get("include_free_games", True)),
                    },
                    ttl=300,
                )
            else:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "library scope must be owned or recent.")
            preferred = ("games",)
            has_more = bool(isinstance(data, dict) and data.get("has_more"))
        elif view == "wishlist":
            data = await self.call(
                "steam_get_wishlist",
                {
                    "steamid": target,
                    "limit": size,
                    "enrich": bool(options.get("enrich", True)),
                    "on_sale_only": bool(options.get("on_sale_only", False)),
                    "country_code": country,
                },
                ttl=300,
            )
            preferred = ("wishlist", "games")
        elif view == "progress":
            if game is None:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "progress view requires game.")
            appid = await self.appid(game, country, language)
            sections = select or ["achievements", "stats", "rarest_unlocks"]
            common = {"steamid": target, "appid": appid, "language": language}
            data = {}
            if "achievements" in sections:
                data["achievements"] = await self.call("steam_get_player_achievements", common)
            if "stats" in sections:
                data["stats"] = await self.call("steam_get_user_game_stats", common)
            if "rarest_unlocks" in sections:
                data["rarest_unlocks"] = await self.call(
                    "steam_get_rarest_unlocks", {**common, "limit": size}
                )
        else:
            data = await self.call(
                "steam_get_inventory",
                {
                    "steamid": target,
                    "appid": int(options.get("appid", 753)),
                    "context_id": options.get("context_id"),
                    "count": size,
                    "language": language,
                },
                ttl=120,
            )
            preferred = ("items", "inventory")

        next_value = self.next_cursor(
            scope=f"player:{view}",
            filters=filters,
            state={"offset": offset + size} if has_more else None,
        )
        return self.result_envelope(
            data,
            canonical_uri=(
                "steam://entity/user/batch"
                if players is not None
                else f"steam://entity/user/{target or 'default'}"
            ),
            provider="steam_web_api",
            preferred_items=preferred,
            next_cursor=next_value,
        )
