"""Protocol-aware Cloud Run smoke using the pinned MCP v2 client."""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

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
        async with Client(transport, mode="auto", read_timeout_seconds=60) as client:
            listing = await client.list_tools(cache_mode="refresh")
            names = [tool.name for tool in listing.tools]
            if names != EXPECTED:
                raise RuntimeError(f"exact tools/list mismatch: {names!r}")
            result = await client.call_tool(
                "steam_game_get",
                {"game": app_id, "view": "summary", "limit": 1},
                read_timeout_seconds=60,
            )
            if result.is_error:
                raise RuntimeError("representative keyless steam_game_get returned isError")

            analysis = await client.call_tool(
                "steam_analyze",
                {
                    "task": "review_insights",
                    "refs": [str(app_id)],
                    "options": {"max_reviews": 1},
                    "request_id": f"cloud-smoke-{uuid.uuid4().hex}",
                },
                read_timeout_seconds=60,
            )
            if analysis.is_error or not analysis.structured_content:
                raise RuntimeError("steam_analyze did not create a Cloud job")
            job_id = str((analysis.structured_content.get("job") or {}).get("job_id") or "")
            if not job_id:
                raise RuntimeError("steam_analyze returned no job_id")
            for _ in range(45):
                status_result = await client.call_tool(
                    "steam_job_get",
                    {"job_id": job_id, "limit": 1, "max_chars": 1_000},
                    read_timeout_seconds=60,
                )
                if status_result.is_error or not status_result.structured_content:
                    raise RuntimeError("steam_job_get failed for the smoke job")
                job = status_result.structured_content.get("job") or {}
                status = str(job.get("status") or "")
                if status == "succeeded":
                    break
                if status in {"failed", "cancelled"}:
                    raise RuntimeError(f"analysis smoke job ended as {status}")
                await asyncio.sleep(2)
            else:
                raise RuntimeError("analysis smoke job did not finish within 90 seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--app-id", type=int, default=570)
    args = parser.parse_args()
    asyncio.run(smoke(args.url, args.app_id))
    print("Steam MCP SDK smoke passed: exact 8 tools, keyless game call, and Cloud job.")


if __name__ == "__main__":
    main()
