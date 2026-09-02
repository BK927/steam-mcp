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
            self._reject_options(kind, options, set())
            ident = self._number(ref)
            data = await self.call(
                "steam_get_package_details", {"packageid": ident, "country_code": country}, ttl=600
            )
            canonical_ref = str(ident)
        elif kind == "workshop":
            self._reject_options(kind, options, set())
            ident = self._number(ref)
            data = await self.call("steam_get_workshop_item", {"published_file_id": ident}, ttl=300)
            canonical_ref = str(ident)
        elif kind == "market":
            allowed_options = {
                "appid",
                "market_hash_name",
                "currency",
                "include_item_details",
            }
            self._reject_options(kind, options, allowed_options)
            raw_appid = options.get("appid")
            if isinstance(raw_appid, bool) or not isinstance(raw_appid, int):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "options.appid must be a positive integer.",
                    schema_uri="steam://schema/steam_community_get.market",
                )
            appid = raw_appid
            market_name = str(options.get("market_hash_name") or ref).strip()
            if appid < 1 or not market_name:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "market requires options.appid and an exact market hash name.",
                )
            currency = options.get("currency", 1)
            if isinstance(currency, bool) or not isinstance(currency, int) or not 1 <= currency <= 41:
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "options.currency must be an integer Steam Market currency code from 1 to 41; ISO values such as KRW are not accepted.",
                    schema_uri="steam://schema/steam_community_get.market",
                )
            include_item_details = options.get("include_item_details", True)
            if not isinstance(include_item_details, bool):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "options.include_item_details must be boolean.",
                    schema_uri="steam://schema/steam_community_get.market",
                )
            data = await self.call(
                "steam_get_market_price",
                {
                    "appid": appid,
                    "market_hash_name": market_name,
                    "currency": currency,
                    "include_item_details": include_item_details,
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
            untrusted_fields=(
                ["data.title", "data.description", "data.tags[]"]
                if kind == "workshop"
                else None
            ),
        )

    @staticmethod
    def _number(value: str) -> int:
        matches = re.findall(r"\d+", str(value))
        if not matches:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "A numeric Steam reference is required.")
        return int(matches[-1])

    @staticmethod
    def _reject_options(kind: str, options: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported {kind} options: {', '.join(unknown)}.",
                schema_uri=f"steam://schema/steam_community_get.{kind}",
            )
