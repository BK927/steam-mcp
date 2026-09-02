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

            achievements = await client.call_tool(
                "steam_game_get",
                {"game": app_id, "view": "achievements", "limit": 1},
                read_timeout_seconds=60,
            )
            achievement_body = achievements.structured_content or {}
            if achievements.is_error or len(achievement_body.get("items") or []) != 1:
                raise RuntimeError("achievement limit did not return exactly one item")
            if not (achievement_body.get("page") or {}).get("next_cursor"):
                raise RuntimeError("achievement page did not return a signed continuation")

            invalid_filter = await client.call_tool(
                "steam_search",
                {
                    "mode": "deals",
                    "filters": {"__unknown_filter": True},
                },
                read_timeout_seconds=60,
            )
            invalid_body = invalid_filter.structured_content or {}
            if not invalid_filter.is_error or invalid_body.get("code") != "INVALID_ARGUMENT":
                raise RuntimeError("unknown deal filter was not rejected")

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
                    data = status_result.structured_content.get("data") or {}
                    meta = status_result.structured_content.get("meta") or {}
                    if data.get("stop_reason") != "max_reviews":
                        raise RuntimeError("review analysis did not report max_reviews")
                    if data.get("partial") is not True or data.get("corpus_complete") is not False:
                        raise RuntimeError("review analysis completeness flags are invalid")
                    if not data.get("continuation_cursor"):
                        raise RuntimeError("review analysis returned no signed continuation")
                    if "data.samples[].review" not in (meta.get("untrusted_fields") or []):
                        raise RuntimeError("review analysis lost its untrusted-field marker")
                    break
                if status in {"failed", "cancelled"}:
                    raise RuntimeError(f"analysis smoke job ended as {status}")
                await asyncio.sleep(2)
            else:
                raise RuntimeError("analysis smoke job did not finish within 90 seconds")

            cancel_job = await client.call_tool(
                "steam_analyze",
                {
                    "task": "review_insights",
                    "refs": [str(app_id)],
                    "options": {"max_reviews": 50_000},
                    "request_id": f"cloud-cancel-smoke-{uuid.uuid4().hex}",
                },
                read_timeout_seconds=60,
            )
            cancel_job_id = str(
                ((cancel_job.structured_content or {}).get("job") or {}).get("job_id") or ""
            )
            if cancel_job.is_error or not cancel_job_id:
                raise RuntimeError("cancellation smoke job was not created")
            cancelled = await client.call_tool(
                "steam_job_cancel",
                {"job_id": cancel_job_id},
                read_timeout_seconds=60,
            )
            if cancelled.is_error:
                raise RuntimeError("steam_job_cancel failed")
            for _ in range(45):
                cancelled_status = await client.call_tool(
                    "steam_job_get",
                    {"job_id": cancel_job_id, "limit": 1, "max_chars": 1_000},
                    read_timeout_seconds=60,
                )
                cancelled_job = (cancelled_status.structured_content or {}).get("job") or {}
                status = str(cancelled_job.get("status") or "")
                if status == "cancelled":
                    if cancelled_job.get("progress") != {"stage": "cancelled"}:
                        raise RuntimeError("cancelled job has the wrong progress stage")
                    if cancelled_job.get("error") is not None:
                        raise RuntimeError("cancelled job retained an error")
                    break
                if status in {"succeeded", "failed"}:
                    raise RuntimeError(f"cancelled smoke job ended as {status}")
                await asyncio.sleep(2)
            else:
                raise RuntimeError("cancelled smoke job did not settle within 90 seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--app-id", type=int, default=570)
    args = parser.parse_args()
    asyncio.run(smoke(args.url, args.app_id))
    print(
        "Steam MCP SDK smoke passed: exact 8 tools, keyless reads, signed paging, "
        "Cloud analysis metadata, and cooperative cancellation."
    )


if __name__ == "__main__":
    main()
