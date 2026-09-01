from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp import Client

from steam_mcp.cache import TtlLruCache
from steam_mcp.contracts import (
    ErrorCode,
    ServiceError,
    collection_envelope,
    compact_size,
    entity_envelope,
    success_result,
)
from steam_mcp.cursor import CursorCodec
from steam_mcp.jobs import InlineJobRunner, MemoryJobStore, MemoryResultStore
from steam_mcp.public_server import (
    PUBLIC_RESOURCE_TEMPLATES,
    PUBLIC_TOOL_NAMES,
    ServerDependencies,
    create_server,
)


def run(value: Any) -> Any:
    return asyncio.run(value)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.review_text = "review"

    async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((operation, arguments))
        if operation == "steam_search_apps":
            return {"results": [{"appid": 10, "name": "Ten"}]}
        if operation == "steam_get_player_summary":
            return {
                "count": len(arguments.get("steamids") or ["default"]),
                "players": [
                    {"steamid": value, "personaname": value}
                    for value in arguments.get("steamids") or ["default"]
                ],
            }
        if operation == "steam_get_app_review_batch":
            return {
                "reviews": [{"review": self.review_text, "voted_up": True}],
                "page": {"has_more": True, "next_cursor": "upstream-2"},
            }
        if operation == "steam_should_i_buy":
            return {"verdict": "yes", "reasons": ["good"]}
        if operation == "steam_get_app_details":
            return {"appid": arguments["appid"], "name": "Game"}
        return {"operation": operation, "arguments": arguments}


def make_server(backend: FakeBackend | None = None, *, clock: Any = None) -> Any:
    backend = backend or FakeBackend()
    cursor = CursorCodec(b"c" * 32, clock=clock or (lambda: 1_000.0))
    runner = InlineJobRunner()
    return create_server(
        ServerDependencies(
            backend=backend,
            cursor=cursor,
            cache=TtlLruCache(max_entries=512, ttl_seconds=600),
            job_store=MemoryJobStore(ttl_seconds=86_400),
            result_store=MemoryResultStore(),
            job_runner=runner,
            status={"job_backend": "memory", "process_role": "mcp"},
        )
    )


def wire(value: Any) -> dict[str, Any]:
    return value.model_dump(by_alias=True, exclude_none=False)


def call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
            return wire(result)

    return run(go())


def resource_text(server: Any, uri: str) -> str:
    return "".join(part.content for part in run(server.read_resource(uri)))


def test_public_registry_exact_surface_and_byte_budgets() -> None:
    server = make_server()
    tools = run(server.list_tools())
    tool_rows = [
        json.dumps(
            tool.model_dump(by_alias=True, exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        for tool in tools
    ]
    assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_NAMES
    assert len(b"[" + b",".join(tool_rows) + b"]") <= 6_000
    assert all(len(row) <= 1_000 for row in tool_rows)
    assert all(len((tool.description or "").encode()) <= 180 for tool in tools)
    assert run(server.list_prompts()) == []
    assert run(server.list_resources()) == []
    templates = tuple(template.uri_template for template in run(server.list_resource_templates()))
    assert templates == PUBLIC_RESOURCE_TEMPLATES


def test_catalog_and_operation_schemas_are_bounded() -> None:
    server = make_server()
    catalog = resource_text(server, "steam://catalog")
    assert len(catalog.encode()) <= 8 * 1024
    assert json.loads(catalog)["tools"] == list(PUBLIC_TOOL_NAMES)
    for operation in PUBLIC_TOOL_NAMES:
        schema = resource_text(server, f"steam://schema/{operation}")
        assert len(schema.encode()) <= 4 * 1024
        assert json.loads(schema)["operation"] == operation


def test_success_and_error_wire_shapes_are_exact() -> None:
    server = make_server()
    result = call(server, "steam_search", {"query": "ten"})
    structured = result["structuredContent"]
    assert set(structured) == {"schema_version", "kind", "data", "items", "job", "page", "meta"}
    assert isinstance(structured["data"], dict)
    assert isinstance(structured["items"], list)
    assert isinstance(structured["job"], dict)
    assert set(structured["meta"]) == {
        "source",
        "provider",
        "retrieved_at",
        "fresh_until",
        "quota_cost",
        "canonical_uri",
        "warnings",
        "untrusted_fields",
    }
    assert structured["meta"]["fresh_until"] is None
    assert structured["meta"]["quota_cost"] is None
    assert len(result["content"]) == 1
    assert len(result["content"][0]["text"].encode()) <= 400

    error = call(server, "steam_search", {"mode": "lookup", "query": ""})
    assert error["isError"] is True
    assert set(error["structuredContent"]) == {
        "code",
        "message",
        "retryable",
        "schema_uri",
        "details",
    }
    assert error["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value


def test_player_array_contract_and_default_user_scope() -> None:
    backend = FakeBackend()
    server = make_server(backend)
    result = call(
        server,
        "steam_player_get",
        {"player": ["alice", "bob"], "view": "profile", "select": ["summary"]},
    )
    assert result["isError"] is False
    assert backend.calls[-1][1]["steamids"] == ["alice", "bob"]

    result = call(server, "steam_player_get", {"view": "profile", "select": ["summary"]})
    assert result["isError"] is False
    assert backend.calls[-1][1]["steamids"] == []

    bad_view = call(
        server,
        "steam_player_get",
        {"player": ["alice"], "view": "library"},
    )
    assert bad_view["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value
    too_many = call(
        server,
        "steam_player_get",
        {"player": [str(index) for index in range(101)], "view": "profile"},
    )
    assert too_many["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value


def test_limits_review_text_and_envelope_utf8_budgets() -> None:
    backend = FakeBackend()
    server = make_server(backend)
    default = call(server, "steam_reviews_get", {"game": 10, "mode": "page"})
    assert default["isError"] is False
    assert backend.calls[-1][1]["max_text_chars"] == 1_200
    capped = call(
        server,
        "steam_reviews_get",
        {"game": 10, "mode": "page", "max_text_chars_per_item": 50_000},
    )
    assert capped["isError"] is False
    assert backend.calls[-1][1]["max_text_chars"] == 4_000
    invalid_limit = call(server, "steam_reviews_get", {"game": 10, "limit": 101})
    assert invalid_limit["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value

    singular = success_result(entity_envelope({"text": "한" * 20_000}), "large")
    assert compact_size(singular.structured_content) <= 12 * 1024
    assert compact_size(singular.structured_content) <= 32 * 1024
    smaller = success_result(
        entity_envelope({"text": "한" * 20_000}),
        "smaller",
        max_bytes=4 * 1024,
    )
    assert compact_size(smaller.structured_content) <= 4 * 1024
    page = collection_envelope(
        [{"text": "한" * 2_000} for _ in range(20)],
        next_cursor="signed-next",
    )
    bounded = success_result(page, "page")
    assert compact_size(bounded.structured_content) <= 12 * 1024
    assert bounded.structured_content["page"]["next_cursor"] == "signed-next"
    no_cursor = success_result(
        collection_envelope([{"text": "x" * 2_000}] * 20),
        "too large",
    ).structured_content
    assert compact_size(no_cursor) <= 12 * 1024
    assert no_cursor["page"]["has_more"] is False
    assert no_cursor["page"]["next_cursor"] is None
    assert no_cursor["meta"]["warnings"]
    truncation = no_cursor["data"]["truncation"]
    assert truncation["original_items"] == 20
    assert truncation["returned_items"] == len(no_cursor["items"])
    assert truncation["omitted_items"] == 20 - len(no_cursor["items"])


def test_runtime_reads_the_configured_result_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import steam_mcp.server as runtime

    monkeypatch.setenv("STEAM_JOB_BACKEND", "memory")
    monkeypatch.setenv("STEAM_MAX_RESULT_BYTES", "4096")
    assert runtime._public_dependencies().max_result_bytes == 4 * 1024


def test_cloud_dependency_branch_can_be_assembled(monkeypatch: pytest.MonkeyPatch) -> None:
    import steam_mcp.server as runtime

    monkeypatch.setattr(runtime, "FirestoreJobStore", lambda **_kwargs: MemoryJobStore())
    monkeypatch.setattr(runtime, "GcsResultStore", lambda _bucket: MemoryResultStore())
    monkeypatch.setattr(runtime, "CloudTasksJobRunner", lambda **_kwargs: InlineJobRunner())
    monkeypatch.setenv("STEAM_JOB_BACKEND", "gcp")
    monkeypatch.setenv("STEAM_CURSOR_SECRET", "s" * 32)
    monkeypatch.setenv("GCP_PROJECT", "example-project")
    monkeypatch.setenv("STEAM_JOB_BUCKET", "example-jobs")
    monkeypatch.setenv("STEAM_JOB_WORKER_URL", "https://worker.example")

    dependencies = runtime._public_dependencies()
    assert dependencies.status["job_backend"] == "gcp"


def test_cursor_hmac_expiry_filter_scope_and_job_expiry_override() -> None:
    now = [100.0]
    codec = CursorCodec(b"s" * 32, ttl_seconds=10, clock=lambda: now[0])
    token = codec.encode(scope="reviews", filters={"game": 10}, state={"offset": 2})
    assert codec.decode(token, scope="reviews", filters={"game": 10}) == {"offset": 2}
    failures = [
        lambda: codec.decode(token + "x", scope="reviews", filters={"game": 10}),
        lambda: codec.decode(token, scope="other", filters={"game": 10}),
        lambda: codec.decode(token, scope="reviews", filters={"game": 11}),
        lambda: codec.decode("not-a-cursor", scope="reviews", filters={"game": 10}),
    ]
    for failure in failures:
        with pytest.raises(ServiceError) as exc:
            failure()
        assert exc.value.code is ErrorCode.CURSOR_MISMATCH
    now[0] = 110.0
    with pytest.raises(ServiceError) as exc:
        codec.decode(token, scope="reviews", filters={"game": 10})
    assert exc.value.code is ErrorCode.CURSOR_MISMATCH

    job_token = codec.encode(
        scope="job:result",
        filters={"job_id": "j"},
        state={"offset": 20},
        expires_at=500.0,
    )
    now[0] = 499.0
    assert codec.decode(job_token, scope="job:result", filters={"job_id": "j"}) == {"offset": 20}
    now[0] = 500.0
    with pytest.raises(ServiceError) as exc:
        codec.decode(job_token, scope="job:result", filters={"job_id": "j"})
    assert exc.value.code is ErrorCode.CURSOR_MISMATCH


def test_cache_lru_ttl_and_inflight_coalescing() -> None:
    now = [0.0]
    cache = TtlLruCache(max_entries=512, ttl_seconds=10, clock=lambda: now[0])

    async def exercise() -> None:
        for index in range(513):
            await cache.set(str(index), index)
        assert cache.size == 512
        assert await cache.get("0") is None
        assert await cache.get("512") == 512
        now[0] = 10.0
        assert await cache.get("512") is None

        calls = 0

        async def loader() -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return "loaded"

        values = await asyncio.gather(*(cache.get_or_load("same", loader) for _ in range(20)))
        assert values == ["loaded"] * 20
        assert calls == 1

    run(exercise())


def test_inline_job_finishes_before_analyze_returns_and_is_idempotent() -> None:
    backend = FakeBackend()
    server = make_server(backend)
    first = call(
        server,
        "steam_analyze",
        {"task": "purchase_decision", "refs": ["10"], "request_id": "same"},
    )
    assert first["structuredContent"]["job"]["status"] == "succeeded"
    job_id = first["structuredContent"]["job"]["job_id"]
    assert first["structuredContent"]["job"]["result_uri"] == f"steam://job/{job_id}/result/_"
    resource = json.loads(resource_text(server, f"steam://job/{job_id}/result/_"))
    assert resource["job"]["job_id"] == job_id
    assert resource["data"]["verdict"] == "yes"
    second = call(
        server,
        "steam_analyze",
        {"task": "purchase_decision", "refs": ["10"], "request_id": "same"},
    )
    assert second["structuredContent"]["job"]["job_id"] == job_id
    collision = call(
        server,
        "steam_analyze",
        {"task": "purchase_decision", "refs": ["11"], "request_id": "same"},
    )
    assert collision["structuredContent"]["code"] == ErrorCode.INVALID_ARGUMENT.value


def test_memory_job_ttl_is_24_hours() -> None:
    now = [1_000.0]
    store = MemoryJobStore(ttl_seconds=86_400, clock=lambda: now[0])
    job = run(store.create("game_overview", ["10"], {}))
    now[0] = job.expires_at
    assert run(store.get(job.job_id)) is None


def assert_backend_calls(
    backend: FakeBackend,
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    assert [operation for operation, _ in backend.calls] == [
        operation for operation, _ in expected
    ]
    for (_, actual), (_, subset) in zip(backend.calls, expected, strict=True):
        assert {key: actual.get(key) for key in subset} == subset


@pytest.mark.parametrize(
    ("view", "extra", "expected"),
    [
        ("summary", {}, [("steam_get_app_details", {"appid": 10})]),
        ("store", {}, [("steam_get_app_details", {"appid": 10})]),
        ("compatibility", {}, [("steam_get_deck_compatibility", {"appid": 10})]),
        (
            "technical",
            {"select": ["product", "branches", "depots", "current_build"]},
            [
                ("steam_get_product_info", {"appid": 10}),
                ("steam_get_branches", {"appid": 10}),
                ("steam_get_depots", {"appid": 10}),
                ("steam_get_current_build", {"appid": 10}),
            ],
        ),
        ("dlc", {}, [("steam_get_dlc", {"appid": 10})]),
        ("tags", {}, [("steam_get_app_tags", {"appid": 10})]),
        (
            "achievements",
            {},
            [
                ("steam_get_game_schema", {"appid": 10}),
                ("steam_get_global_achievement_percentages", {"appid": 10}),
            ],
        ),
        ("live", {}, [("steam_get_current_players", {"appid": 10})]),
        ("news", {}, [("steam_get_app_news", {"appid": 10})]),
        ("pricing", {}, [("steam_get_app_regional_pricing", {"appid": 10})]),
    ],
)
def test_all_game_views_route_to_legacy_operations(
    view: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(make_server(backend), "steam_game_get", {"game": 10, "view": view, **extra})
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


@pytest.mark.parametrize(
    ("view", "extra", "expected"),
    [
        (
            "profile",
            {},
            [
                ("steam_get_player_summary", {"steamids": ["alice"]}),
                ("steam_get_steam_level", {"steamid": "alice"}),
                ("steam_get_player_bans", {"steamid": "alice"}),
                ("steam_get_player_badges", {"steamid": "alice"}),
            ],
        ),
        (
            "social",
            {},
            [
                ("steam_get_friend_list", {"steamid": "alice"}),
                ("steam_get_user_groups", {"steamid": "alice"}),
            ],
        ),
        ("library", {}, [("steam_get_owned_games", {"steamid": "alice"})]),
        ("wishlist", {}, [("steam_get_wishlist", {"steamid": "alice"})]),
        (
            "progress",
            {"game": 10},
            [
                ("steam_get_player_achievements", {"steamid": "alice", "appid": 10}),
                ("steam_get_user_game_stats", {"steamid": "alice", "appid": 10}),
                ("steam_get_rarest_unlocks", {"steamid": "alice", "appid": 10}),
            ],
        ),
        ("inventory", {}, [("steam_get_inventory", {"steamid": "alice", "appid": 753})]),
    ],
)
def test_all_player_views_route_to_legacy_operations(
    view: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_player_get",
        {"player": "alice", "view": view, **extra},
    )
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


@pytest.mark.parametrize(
    ("mode", "extra", "expected"),
    [
        ("lookup", {"query": "ten"}, [("steam_search_apps", {"query": "ten"})]),
        ("discover", {}, [("steam_discover", {"term": None})]),
        ("deals", {}, [("steam_get_featured_specials", {"limit": 10})]),
        ("chart", {}, [("steam_get_store_highlights", {"section": "top_sellers"})]),
    ],
)
def test_all_search_modes_route_to_legacy_operations(
    mode: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(make_server(backend), "steam_search", {"mode": mode, **extra})
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("summary", [("steam_get_app_reviews", {"appid": 10, "limit": 20})]),
        (
            "page",
            [("steam_get_app_review_batch", {"appid": 10, "max_text_chars": 1_200})],
        ),
    ],
)
def test_all_review_modes_route_to_legacy_operations(
    mode: str,
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_reviews_get",
        {"game": 10, "mode": mode},
    )
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


@pytest.mark.parametrize(
    ("kind", "extra", "expected"),
    [
        ("package", {}, [("steam_get_package_details", {"packageid": 123})]),
        ("workshop", {}, [("steam_get_workshop_item", {"published_file_id": 123})]),
        (
            "market",
            {"options": {"appid": 730, "market_hash_name": "Item"}},
            [("steam_get_market_price", {"appid": 730, "market_hash_name": "Item"})],
        ),
    ],
)
def test_all_community_kinds_route_to_legacy_operations(
    kind: str,
    extra: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_community_get",
        {"kind": kind, "ref": "123", **extra},
    )
    assert result["isError"] is False
    assert_backend_calls(backend, expected)


@pytest.mark.parametrize(
    ("task", "refs", "options", "expected"),
    [
        (
            "friend_ownership",
            ["alice", "10"],
            {},
            [("steam_find_friends_who_own", {"steamid": "alice", "appid": 10})],
        ),
        (
            "review_insights",
            ["10"],
            {"max_reviews": 1},
            [("steam_get_app_review_batch", {"appid": 10, "page_size": 1})],
        ),
        (
            "game_overview",
            ["10"],
            {},
            [
                ("steam_get_app_details", {"appid": 10}),
                ("steam_get_app_tags", {"appid": 10}),
                ("steam_get_app_reviews", {"appid": 10}),
                ("steam_get_current_players", {"appid": 10}),
                ("steam_get_app_news", {"appid": 10}),
                ("steam_get_product_info", {"appid": 10}),
            ],
        ),
        (
            "player_compare",
            ["alice", "bob"],
            {},
            [("steam_compare_players", {"steamid_a": "alice", "steamid_b": "bob"})],
        ),
        (
            "library_insights",
            ["alice"],
            {},
            [("steam_analyze_library", {"steamid": "alice"})],
        ),
        (
            "purchase_decision",
            ["10"],
            {},
            [("steam_should_i_buy", {"appid": 10})],
        ),
        (
            "recommendations",
            ["10"],
            {},
            [("steam_recommend", {"seed_appid": 10})],
        ),
        (
            "coop_plan",
            ["alice", "bob"],
            {},
            [("steam_plan_coop_night", {"steamid": "alice", "friends": ["bob"]})],
        ),
    ],
)
def test_all_analysis_tasks_route_to_legacy_operations(
    task: str,
    refs: list[str],
    options: dict[str, Any],
    expected: list[tuple[str, dict[str, Any]]],
) -> None:
    backend = FakeBackend()
    result = call(
        make_server(backend),
        "steam_analyze",
        {"task": task, "refs": refs, "options": options},
    )
    assert result["isError"] is False
    assert result["structuredContent"]["job"]["status"] == "succeeded"
    assert_backend_calls(backend, expected)
