"""Package, Workshop, and Community Market reads."""

from __future__ import annotations

import re
from typing import Any

from ..contracts import ErrorCode, ServiceError
from .base import BaseService, locale_values


class CommunityService(BaseService):
    async def get(
        self,
        kind: str,
        ref: str,
        options: dict[str, Any],
        locale: dict[str, Any],
    ) -> dict[str, Any]:
        _language, country = locale_values(locale)
        if kind == "package":
            ident = self._number(ref)
            data = await self.call(
                "steam_get_package_details", {"packageid": ident, "country_code": country}, ttl=600
            )
            canonical_ref = str(ident)
        elif kind == "workshop":
            ident = self._number(ref)
            data = await self.call("steam_get_workshop_item", {"published_file_id": ident}, ttl=300)
            canonical_ref = str(ident)
        elif kind == "market":
            appid = int(options.get("appid") or 0)
            market_name = str(options.get("market_hash_name") or ref).strip()
            if appid < 1 or not market_name:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "market requires options.appid and an exact market hash name.",
                )
            data = await self.call(
                "steam_get_market_price",
                {
                    "appid": appid,
                    "market_hash_name": market_name,
                    "currency": int(options.get("currency", 1)),
                    "include_item_details": bool(options.get("include_item_details", True)),
                },
                ttl=60,
            )
            canonical_ref = f"{appid}/{market_name}"
        else:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported community kind: {kind}.",
                schema_uri=f"steam://schema/steam_community_get.{kind}",
            )
        return self.result_envelope(
            data,
            canonical_uri=f"steam://entity/{kind}/{canonical_ref}",
            provider="steam_community",
        )

    @staticmethod
    def _number(value: str) -> int:
        matches = re.findall(r"\d+", str(value))
        if not matches:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "A numeric Steam reference is required.")
        return int(matches[-1])
