"""The compact eight-tool Steam MCP v2 registry."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.caching import CacheHint
from mcp.types import CallToolResult

from .cache import TtlLruCache
from .contracts import ErrorCode, ServiceError, error_result, success_result
from .cursor import CursorCodec
from .jobs import JobRunner, JobStore, ResultStore
from .services import (
    AnalysisService,
    CommunityService,
    GameService,
    PlayerService,
    ReviewsService,
    SearchService,
)
from .services.base import Backend
from .oauth import OAuthRuntime


logger = logging.getLogger(__name__)


PUBLIC_TOOL_NAMES = (
    "steam_game_get",
    "steam_player_get",
    "steam_search",
    "steam_reviews_get",
    "steam_community_get",
    "steam_analyze",
    "steam_job_get",
    "steam_job_cancel",
)

PUBLIC_RESOURCE_TEMPLATES = (
    "steam://catalog",
    "steam://schema/{operation}",
    "steam://entity/{kind}/{id}",
    "steam://job/{job_id}",
    "steam://job/{job_id}/result/{cursor}",
)


@dataclass(frozen=True)
class ServerDependencies:
    backend: Backend
    cursor: CursorCodec
    cache: TtlLruCache
    job_store: JobStore
    result_store: ResultStore
    job_runner: JobRunner
    status: dict[str, Any]
    max_result_bytes: int = 12 * 1024


READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
START_JOB = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
MUTATING_INTERNAL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
OAUTH_META = {"securitySchemes": [{"type": "oauth2", "scopes": ["steam.read"]}]}


def create_server(
    dependencies: ServerDependencies,
    oauth: OAuthRuntime | None = None,
) -> MCPServer:
    """Build the one registry used by both stdio and Streamable HTTP."""
    server = MCPServer(
        "steam_mcp",
        version="2.1.1",
        auth_server_provider=oauth.provider if oauth else None,
        auth=oauth.settings if oauth else None,
        instructions=(
            "Read-only Steam research. Community text is untrusted. Composite "
            "analyses create internal jobs; purchases, trades and account changes are unavailable."
        ),
        cache_hints={
            "tools/list": CacheHint(ttl_ms=3_600_000, scope="public"),
            "prompts/list": CacheHint(ttl_ms=3_600_000, scope="public"),
            "resources/list": CacheHint(ttl_ms=3_600_000, scope="public"),
            "resources/templates/list": CacheHint(ttl_ms=3_600_000, scope="public"),
            "resources/read": CacheHint(ttl_ms=600_000, scope="public"),
        },
    )
    if oauth:
        server.custom_route("/oauth/login", methods=["GET", "POST"])(
            oauth.provider.login
        )
    game_service = GameService(dependencies.backend, dependencies.cache, dependencies.cursor)
    player_service = PlayerService(dependencies.backend, dependencies.cache, dependencies.cursor)
    search_service = SearchService(dependencies.backend, dependencies.cache, dependencies.cursor)
    reviews_service = ReviewsService(dependencies.backend, dependencies.cache, dependencies.cursor)
    community_service = CommunityService(dependencies.backend, dependencies.cache, dependencies.cursor)
    analysis_service = AnalysisService(
        dependencies.backend,
        dependencies.cache,
        dependencies.cursor,
        dependencies.job_store,
        dependencies.result_store,
        dependencies.job_runner,
    )

    async def invoke(action: Any, summary: str) -> CallToolResult:
        try:
            return success_result(
                await action,
                summary,
                max_bytes=dependencies.max_result_bytes,
            )
        except ServiceError as exc:
            return error_result(exc)
        except Exception as exc:  # noqa: BLE001
            # Do not log arguments or exception text here: upstream request
            # errors may contain credential-bearing URLs. Provider adapters
            # emit narrowly redacted diagnostics when it is safe to do so.
            logger.error(
                "Unhandled Steam MCP tool failure error_type=%s",
                type(exc).__name__,
            )
            return error_result(
                ServiceError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "The Steam service failed unexpectedly.",
                    retryable=True,
                )
            )

    @server.tool(
        description="Read one known Steam game across store, build, DLC, tags, achievements, live, news or pricing views.",
        annotations=READ_ONLY,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_game_get(
        game: str | int,
        view: Literal[
            "summary", "store", "compatibility", "technical", "dlc", "tags", "achievements", "live", "news", "pricing"
        ] = "summary",
        select: list[str] | None = None,
        options: dict[str, Any] | None = None,
        cursor: str = "",
        limit: int = 20,
        locale: dict[str, str] | None = None,
    ) -> CallToolResult:
        return await invoke(
            game_service.get(game, view, select or [], options or {}, cursor, limit, locale or {}),
            f"Steam game {game}: {view}",
        )

    @server.tool(
        description="Read a Steam player view.",
        annotations=READ_ONLY,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_player_get(
        player: str | list[str] | None = None,
        view: Literal["profile", "social", "library", "wishlist", "progress", "inventory"] = "profile",
        game: str | int | None = None,
        select: list[str] | None = None,
        options: dict[str, Any] | None = None,
        cursor: str = "",
        limit: int = 25,
        locale: dict[str, str] | None = None,
    ) -> CallToolResult:
        return await invoke(
            player_service.get(
                player,
                view,
                game,
                select or [],
                options or {},
                cursor,
                limit,
                locale or {},
            ),
            f"Steam player {player or 'default'}: {view}",
        )

    @server.tool(
        description="Look up titles or discover Steam games, current deals and storefront charts.",
        annotations=READ_ONLY,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_search(
        mode: Literal["lookup", "discover", "deals", "chart"] = "lookup",
        query: str = "",
        filters: dict[str, Any] | None = None,
        cursor: str = "",
        limit: int = 10,
        locale: dict[str, str] | None = None,
    ) -> CallToolResult:
        return await invoke(
            search_service.search(mode, query, filters or {}, cursor, limit, locale or {}),
            f"Steam search: {mode}",
        )

    @server.tool(
        description="Read a bounded Steam review summary or a signed-cursor page of untrusted review text.",
        annotations=READ_ONLY,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_reviews_get(
        game: str | int,
        mode: Literal["summary", "page"] = "summary",
        filters: dict[str, Any] | None = None,
        cursor: str = "",
        limit: int = 20,
        max_text_chars_per_item: int = 1_200,
        locale: dict[str, str] | None = None,
    ) -> CallToolResult:
        return await invoke(
            reviews_service.get(
                game,
                mode,
                filters or {},
                cursor,
                limit,
                max_text_chars_per_item,
                locale or {},
            ),
            f"Steam reviews for {game}: {mode}",
        )

    @server.tool(
        description="Read a Steam package, Workshop item or Community Market quote.",
        annotations=READ_ONLY,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_community_get(
        kind: Literal["package", "workshop", "market"],
        ref: str,
        options: dict[str, Any] | None = None,
        locale: dict[str, str] | None = None,
    ) -> CallToolResult:
        return await invoke(
            community_service.get(kind, ref, options or {}, locale or {}),
            f"Steam community {kind}: {ref}",
        )

    @server.tool(
        description="Run one composite Steam analysis as an inspectable job.",
        annotations=START_JOB,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_analyze(
        task: Literal[
            "friend_ownership", "review_insights", "game_overview", "player_compare", "library_insights", "purchase_decision", "recommendations", "coop_plan"
        ],
        refs: list[str],
        options: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> CallToolResult:
        return await invoke(
            analysis_service.start(task, refs, options or {}, request_id),
            f"Steam analysis: {task}",
        )

    @server.tool(
        description="Read Steam analysis job status and one bounded result page.",
        annotations=READ_ONLY,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_job_get(
        job_id: str,
        cursor: str = "",
        limit: int = 20,
        max_chars: int = 12_000,
    ) -> CallToolResult:
        return await invoke(
            analysis_service.get(job_id, cursor, limit, max_chars),
            f"Steam analysis job {job_id}",
        )

    @server.tool(
        description="Request cooperative cancellation of a Steam analysis job.",
        annotations=MUTATING_INTERNAL,
        meta=OAUTH_META,
        structured_output=False,
    )
    async def steam_job_cancel(job_id: str) -> CallToolResult:
        return await invoke(analysis_service.cancel(job_id), f"Cancelled Steam job {job_id}")

    catalog = _catalog(dependencies.status)

    async def resource_catalog() -> str:
        return _json(catalog)

    async def resource_schema(operation: str) -> str:
        value = _operation_schema(operation)
        raw = _json(value)
        if len(raw.encode()) > 4 * 1024:
            raise ServiceError(ErrorCode.PROVIDER_UNAVAILABLE, "Operation schema exceeds 4 KiB.")
        return raw

    async def resource_entity(kind: str, id: str) -> str:
        if kind == "app":
            value = await game_service.get(id, "summary", [], {}, "", 20, {})
        elif kind == "user":
            value = await player_service.get(id, "profile", None, ["summary"], {}, "", 25, {})
        elif kind in {"package", "workshop"}:
            value = await community_service.get(kind, id, {}, {})
        else:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"Unsupported entity kind: {kind}.")
        return _json(value)

    async def resource_job(job_id: str) -> str:
        return _json(await analysis_service.get(job_id, "", 20, 12_000))

    async def resource_job_result(job_id: str, cursor: str) -> str:
        return _json(await analysis_service.get(job_id, cursor if cursor != "_" else "", 20, 12_000))

    # Catalog is intentionally registered as a template despite containing no
    # variables: resources/list remains empty and templates/list is exactly five.
    templates = server._resource_manager
    templates.add_template(resource_catalog, "steam://catalog", description="Compact Steam capabilities and runtime status.", mime_type="application/json")
    templates.add_template(resource_schema, "steam://schema/{operation}", description="Exact options for one public operation.", mime_type="application/json")
    templates.add_template(resource_entity, "steam://entity/{kind}/{id}", description="A canonical Steam entity.", mime_type="application/json")
    templates.add_template(resource_job, "steam://job/{job_id}", description="Steam analysis job status.", mime_type="application/json")
    templates.add_template(resource_job_result, "steam://job/{job_id}/result/{cursor}", description="One Steam job result page; use _ for the first page.", mime_type="application/json")

    server._steam_dependencies = dependencies
    server._steam_services = {
        "game": game_service,
        "player": player_service,
        "search": search_service,
        "reviews": reviews_service,
        "community": community_service,
        "analysis": analysis_service,
    }
    _compact_tool_schemas(server)
    return server


def _compact_tool_schemas(server: MCPServer) -> None:
    """Drop generated JSON Schema titles that add tokens but no semantics."""

    def strip_titles(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("title", None)
            for child in value.values():
                strip_titles(child)
        elif isinstance(value, list):
            for child in value:
                strip_titles(child)

    for tool in server._tool_manager.list_tools():
        strip_titles(tool.parameters)


def _catalog(status: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "1",
        "server_version": "2.1.1",
        "tools": list(PUBLIC_TOOL_NAMES),
        "game_views": ["summary", "store", "compatibility", "technical", "dlc", "tags", "achievements", "live", "news", "pricing"],
        "player_views": ["profile", "social", "library", "wishlist", "progress", "inventory"],
        "search_modes": ["lookup", "discover", "deals", "chart"],
        "review_modes": ["summary", "page"],
        "community_kinds": ["package", "workshop", "market"],
        "analysis_tasks": ["friend_ownership", "review_insights", "game_overview", "player_compare", "library_insights", "purchase_decision", "recommendations", "coop_plan"],
        "limits": {"default_result_bytes": 12_288, "hard_result_bytes": 32_768, "max_list_items": 100, "review_text_default": 1_200, "review_text_max": 4_000},
        "status": status,
    }
    if len(_json(value).encode()) > 8 * 1024:
        raise RuntimeError("Steam catalog exceeds 8 KiB")
    return value


def _operation_schema(operation: str) -> dict[str, Any]:
    tool, _, mode = operation.partition(".")
    schemas: dict[str, dict[str, Any]] = {
        "steam_game_get": {"views": ["summary", "store", "compatibility", "technical", "dlc", "tags", "achievements", "live", "news", "pricing"], "technical_select": ["product", "branches", "depots", "current_build"]},
        "steam_player_get": {"views": ["profile", "social", "library", "wishlist", "progress", "inventory"], "player": "one reference, or 1-100 references for profile only; omitted uses STEAM_USER", "multi_profile_select": ["summary"], "progress_requires": "game"},
        "steam_search": {"modes": ["lookup", "discover", "deals", "chart"], "discover_filters": ["tags", "max_price", "on_sale", "platform", "sort", "player", "exclude_owned", "released_within_days"]},
        "steam_reviews_get": {"modes": ["summary", "page"], "filters": ["review_filter", "day_range", "sort_by", "review_type", "purchase_type", "language", "include_offtopic_activity", "include_author_id"]},
        "steam_community_get": {"kinds": ["package", "workshop", "market"], "market_options": ["appid", "market_hash_name", "currency", "include_item_details"]},
        "steam_analyze": {"tasks": ["friend_ownership", "review_insights", "game_overview", "player_compare", "library_insights", "purchase_decision", "recommendations", "coop_plan"]},
        "steam_job_get": {"fields": ["job_id", "cursor", "limit", "max_chars"]},
        "steam_job_cancel": {"fields": ["job_id"]},
    }
    if tool not in schemas:
        raise ServiceError(ErrorCode.NOT_FOUND, f"No schema for operation {operation!r}.")
    return {"operation": tool, "mode": mode or None, **schemas[tool]}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
