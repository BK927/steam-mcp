"""Small public-path smoke for a standalone Raspberry Pi Steam MCP."""

from __future__ import annotations

import argparse
import asyncio
import os

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


EXPECTED = [
    "steam_game_get",
    "steam_player_get",
    "steam_search",
    "steam_reviews_get",
    "steam_community_get",
    "steam_analyze",
    "steam_job_get",
    "steam_job_cancel",
]


async def smoke(url: str, app_id: int) -> None:
    token = os.environ.get("MCP_SMOKE_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("MCP_SMOKE_ACCESS_TOKEN is required")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, mode="auto", read_timeout_seconds=90) as client:
            listing = await client.list_tools(cache_mode="refresh")
            names = [tool.name for tool in listing.tools]
            if names != EXPECTED:
                raise RuntimeError(f"exact tools/list mismatch: {names!r}")

            summary = await client.call_tool(
                "steam_game_get",
                {"game": app_id, "view": "summary", "limit": 1},
                read_timeout_seconds=90,
            )
            if summary.is_error:
                raise RuntimeError("representative Steam summary failed")

            analytics = await client.call_tool(
                "steam_game_get",
                {
                    "game": app_id,
                    "view": "analytics",
                    "options": {"providers": ["steam", "gamalytic", "steamspy"]},
                    "limit": 1,
                },
                read_timeout_seconds=90,
            )
            body = analytics.structured_content or {}
            data = body.get("data") or {}
            sources = data.get("sources") or {}
            availability = data.get("availability") or {}
            if analytics.is_error or "steam" not in sources or "gamalytic" not in sources:
                raise RuntimeError(
                    "analytics did not return both official Steam and Gamalytic sources"
                )
            steamspy = availability.get("steamspy") or {}
            if steamspy.get("status") not in {"available", "unavailable"}:
                raise RuntimeError("SteamSpy did not report an explicit availability state")
            print(
                "SMOKE_OK "
                f"tools={len(names)} "
                f"sources={','.join(sources)} "
                f"steamspy={steamspy.get('status')}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--app-id", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(smoke(args.url, args.app_id))


if __name__ == "__main__":
    main()
