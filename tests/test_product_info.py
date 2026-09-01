"""Current SteamCMD AppInfo normalization and stateless analysis tools."""

import asyncio
import json

import steam_mcp.product_info as P
import steam_mcp.legacy_backend as S


def run(coro):
    return asyncio.run(coro)


def app_info(appid=42):
    return {
        "_change_number": 123456,
        "_missing_token": False,
        "_sha": "abc123",
        "_size": 4096,
        "appid": str(appid),
        "common": {
            "name": "Test Game",
            "type": "Game",
            "releasestate": "released",
            "oslist": "windows,linux",
            "controller_support": "full",
            "community_visible_stats": "1",
            "languages": {"english": "1", "koreana": "1", "unused": "0"},
            "associations": {
                "0": {"name": "Dev Studio", "type": "developer"},
                "1": {"name": "Pub Studio", "type": "publisher"},
            },
            "category": {"category_1": "1", "category_9": "1"},
            "genres": {"0": "23", "1": "3"},
            "primary_genre": "23",
            "steam_release_date": "1700000000",
        },
        "config": {
            "contenttype": "3",
            "installdir": "TestGame",
            "launch": {
                "0": {
                    "executable": "game.exe",
                    "arguments": "-windowed",
                    "description": "Play",
                    "config": {"oslist": "windows", "osarch": "64"},
                }
            },
        },
        "depots": {
            "100": {
                "name": "Windows Depot",
                "config": {"oslist": "windows", "osarch": "64"},
                "manifests": {
                    "public": {"gid": "111", "size": "1000", "download": "700"},
                    "beta": "112",
                },
                "encryptedmanifests": {"private": {"encrypted_gid_2": "SECRET"}},
            },
            "101": {
                "name": "Linux Depot",
                "config": {"oslist": "linux"},
                "manifests": {"public": "211"},
            },
            "102": {
                "config": {},
                "depotfromapp": "7",
                "sharedinstall": "1",
            },
            "branches": {
                "public": {
                    "buildid": "9001",
                    "timeupdated": "1700000100",
                    "timebuildupdated": "1700000200",
                },
                "beta": {
                    "buildid": "9000",
                    "description": "Previous test build",
                    "pwdrequired": "1",
                    "timeupdated": "1690000000",
                },
            },
            "baselanguages": "english,koreana",
            "privatebranches": {"internal": "hidden-value"},
        },
        "extended": {"developer": "Dev Studio", "publisher": "Pub Studio"},
        "ufs": {"quota": "100000"},
    }


def api_payload(appid=42):
    return {"status": "success", "data": {str(appid): app_info(appid)}}


def test_extract_app_info_rejects_empty_success_payload():
    payload = {"status": "success", "data": {"42": {}}}
    try:
        P.extract_app_info(payload, 42)
    except ValueError as exc:
        assert "did not contain app 42" in str(exc)
    else:
        raise AssertionError("empty app-info payload must not look like a valid app")


def test_product_info_normalization_is_current_and_compact():
    app = P.extract_app_info(api_payload(), 42)
    result = P.normalize_product_overview(
        app, appid=42, branch="public", include_launch_options=True
    )

    assert result["change_number"] == 123456
    assert result["common"]["languages"] == ["english", "koreana"]
    assert result["common"]["category_ids"] == [1, 9]
    assert result["counts"] == {
        "branches": 2,
        "depots": 3,
        "launch_options": 1,
        "password_required_branches": 1,
    }
    assert result["selected_branch"]["build_id"] == "9001"
    assert result["selected_branch"]["manifest_count"] == 2
    assert result["selected_branch"]["reported_manifest_size_bytes"] == 1000
    assert result["history_available"] is False
    assert result["launch_options"][0]["os"] == ["windows"]


def test_depots_hide_encrypted_values_and_support_platform_filtering():
    depots = P.normalize_depots(app_info(), branch="public", include_all_manifests=True)
    windows = [row for row in depots if P.depot_matches_platform(row, "windows")]

    assert [row["depot_id"] for row in windows] == [100, 102]
    first = windows[0]
    assert first["selected_manifest"] == {
        "gid": "111",
        "size_bytes": 1000,
        "download_bytes": 700,
    }
    assert first["manifests"]["beta"]["gid"] == "112"
    assert first["has_encrypted_manifests"] is True
    assert "SECRET" not in json.dumps(depots)


def test_build_snapshot_keeps_shared_depots_and_visible_manifests():
    build = P.normalize_build_snapshot(
        app_info(), appid=42, branch="public", platform="windows"
    )

    assert build["available"] is True
    assert build["build_id"] == "9001"
    assert build["depots_considered"] == 2  # windows + untagged shared depot
    assert build["manifest_count"] == 1
    assert build["depots_without_selected_manifest"] == 1
    assert build["reported_download_bytes"] == 700
    assert build["history_available"] is False


def test_mirror_http_errors_are_not_mislabelled_as_valve_errors():
    request = S.httpx2.Request("GET", "https://api.steamcmd.net/v1/info/42")
    response = S.httpx2.Response(404, request=request)
    error = S.httpx2.HTTPStatusError("not found", request=request, response=response)
    assert "SteamCMD mirror" in S._handle_error(error)

    timeout = S.httpx2.ReadTimeout("slow", request=request)
    assert "SteamCMD AppInfo mirror timed out" in S._handle_error(timeout)


def test_product_info_tool_fetches_keyless_community_mirror(monkeypatch):
    captured = {}

    async def fake_raw(url, params, cache_ttl=0):
        captured.update({"url": url, "params": params, "ttl": cache_ttl})
        return api_payload()

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    out = run(
        S.steam_get_product_info(
            S.ProductInfoInput(
                appid=42, include_launch_options=True, response_format="json"
            )
        )
    )
    data = json.loads(out)

    assert captured["url"] == "https://api.steamcmd.net/v1/info/42"
    assert captured["params"] == {}
    assert captured["ttl"] == S.CACHE_TTL_PRODUCT_INFO
    assert data["source"]["provider"] == "steamcmd.net"
    assert data["source"]["api_key_required"] is False
    assert data["source"]["mcp_persistent_storage"] is False
    assert data["source"]["mcp_memory_cache_ttl_seconds"] == 300
    assert data["source"]["provider_maintains_external_appinfo_database"] is True
    assert data["source"]["content_trust"] == "untrusted_external_data"
    assert data["selected_branch"]["build_id"] == "9001"


def test_branches_depots_and_build_tools_page_and_filter(monkeypatch):
    async def fake_info(appid):
        return app_info(appid)

    monkeypatch.setattr(S, "_steamcmd_app_info", fake_info)

    branches = json.loads(
        run(
            S.steam_get_branches(
                S.AppBranchesInput(appid=42, limit=1, response_format="json")
            )
        )
    )
    assert branches["branches"][0]["name"] == "public"
    assert branches["next_offset"] == 1

    depots = json.loads(
        run(
            S.steam_get_depots(
                S.AppDepotsInput(
                    appid=42,
                    platform="linux",
                    include_all_manifests=True,
                    response_format="json",
                )
            )
        )
    )
    # Linux-specific depot plus untagged shared depot.
    assert [row["depot_id"] for row in depots["depots"]] == [101, 102]

    build = json.loads(
        run(
            S.steam_get_current_build(
                S.AppBuildInput(appid=42, platform="windows", response_format="json")
            )
        )
    )
    assert build["build_id"] == "9001"
    assert build["manifest_total"] == 1
    assert build["manifests"][0]["gid"] == "111"


def test_analyze_game_combines_keyless_sources(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {
            "42": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "type": "game",
                    "is_free": False,
                    "price_overview": {
                        "final_formatted": "$9.99",
                        "initial_formatted": "$19.99",
                        "discount_percent": 50,
                    },
                    "developers": ["Dev Studio"],
                    "publishers": ["Pub Studio"],
                    "release_date": {"date": "2024", "coming_soon": False},
                    "genres": [{"description": "Indie"}],
                    "categories": [
                        {"description": "Single-player"},
                        {"description": "Online Co-op"},
                    ],
                    "platforms": {"windows": True, "linux": True, "mac": False},
                    "controller_support": "full",
                    "metacritic": {"score": 80},
                    "recommendations": {"total": 1234},
                    "achievements": {"total": 20},
                    "dlc": [1000, 1001],
                    "short_description": "A test game.",
                },
            }
        }

    async def fake_info(appid):
        return app_info(appid)

    async def fake_review_summary(*args, **kwargs):
        return {
            "success": 1,
            "query_summary": {
                "review_score": 8,
                "review_score_desc": "Very Positive",
                "total_positive": 900,
                "total_negative": 100,
                "total_reviews": 1000,
            },
        }

    async def fake_recent(*args, **kwargs):
        return {
            "day_range": 30,
            "reviews_counted": 100,
            "positive": 80,
            "negative": 20,
            "positive_pct": 80.0,
            "sampled": True,
            "partial": True,
            "complete_for_requested_scope": False,
            "stop_reason": "max_reviews",
            "next_cursor": "next",
            "error": None,
            "scan_limit": 100,
            "pages_fetched": 1,
            "elapsed_seconds": 0.1,
            "newest_timestamp": 1700000000,
            "newest_at": "2023-11-14T00:00:00Z",
            "oldest_timestamp": 1699990000,
            "oldest_at": "2023-11-13T00:00:00Z",
            "malformed_timestamps_skipped": 0,
            "samples": [],
            "scope": {
                "language": "all",
                "purchase_type": "steam",
                "offtopic_activity_included": False,
            },
        }

    async def fake_items(appids):
        return {42: [{"tagid": 1, "weight": 100}]}

    async def fake_tag_names():
        return {1: "Indie"}

    async def fake_steam(path, params, **kwargs):
        if "GetNumberOfCurrentPlayers" in path:
            return {"response": {"result": 1, "player_count": 321}}
        if "GetNewsForApp" in path:
            return {
                "appnews": {
                    "newsitems": [
                        {
                            "title": "Patch 1.1",
                            "date": 1700000000,
                            "feedlabel": "Community Announcements",
                            "url": "https://example.invalid/news",
                            "contents": "Fixed things.",
                        }
                    ]
                }
            }
        raise AssertionError(path)

    async def fake_deck(appid, language):
        return {"category": 3, "label": "Verified", "items": [], "blog_url": None}

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_steamcmd_app_info", fake_info)
    monkeypatch.setattr(S, "_review_summary_query", fake_review_summary)
    monkeypatch.setattr(S, "_scan_recent_reviews", fake_recent)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_tag_name_map", fake_tag_names)
    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_deck_compat", fake_deck)

    out = run(
        S.steam_analyze_game(
            S.GameAnalysisInput(
                appid=42,
                platform="windows",
                review_max_reviews=100,
                response_format="json",
            )
        )
    )
    data = json.loads(out)

    assert data["store"]["price"] == "$9.99"
    assert data["store"]["features"]["online_coop"] is True
    assert data["reviews"]["official_lifetime"]["positive_pct"] == 90.0
    assert data["reviews"]["official_recent"]["positive_pct"] == 80.0
    assert data["reviews"]["trend_points_vs_lifetime"] == -10.0
    assert data["current_players"] == 321
    assert data["community_tags"][0]["tag"] == "Indie"
    assert data["technical"]["selected_branch"]["build_id"] == "9001"
    assert data["technical"]["selected_branch"]["platform"] == "windows"
    assert data["technical"]["selected_branch"]["depots_considered"] == 2
    assert data["news"][0]["title"] == "Patch 1.1"
    assert data["signals"]["recent_score_drop_5pts"] is True
    assert data["source_errors"] == {}


def test_analyze_game_keeps_other_results_when_product_mirror_fails(monkeypatch):
    mirror_calls = 0

    async def fake_store(path, params, cache_ttl=0):
        return {"42": {"success": True, "data": {"name": "Test Game"}}}

    async def broken_info(appid):
        nonlocal mirror_calls
        mirror_calls += 1
        raise S.SteamApiError("mirror unavailable")

    async def fake_review_summary(*args, **kwargs):
        return {"success": 1, "query_summary": {}}

    async def fake_recent(*args, **kwargs):
        return {
            "reviews_counted": 0,
            "positive_pct": 0.0,
            "sampled": False,
            "partial": False,
            "stop_reason": "api_exhausted",
            "error": None,
        }

    async def fake_items(appids):
        return {}

    async def fake_names():
        return {}

    async def fake_steam(path, params, **kwargs):
        if "GetNumberOfCurrentPlayers" in path:
            return {"response": {"result": 1, "player_count": 1}}
        return {"appnews": {"newsitems": []}}

    async def fake_deck(appid, language):
        return None

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_steamcmd_app_info", broken_info)
    monkeypatch.setattr(S, "_review_summary_query", fake_review_summary)
    monkeypatch.setattr(S, "_scan_recent_reviews", fake_recent)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_tag_name_map", fake_names)
    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_deck_compat", fake_deck)

    data = json.loads(
        run(
            S.steam_analyze_game(
                S.GameAnalysisInput(appid=42, news_count=0, response_format="json")
            )
        )
    )

    assert data["store"]["name"] == "Test Game"
    assert data["current_players"] == 1
    assert data["technical"] is None
    assert "mirror unavailable" in data["source_errors"]["technical"]
    assert mirror_calls == 1

    valve_only = json.loads(
        run(
            S.steam_analyze_game(
                S.GameAnalysisInput(
                    appid=42,
                    news_count=0,
                    include_technical=False,
                    response_format="json",
                )
            )
        )
    )
    assert valve_only["snapshot_scope"]["technical_included"] is False
    assert valve_only["technical"] is None
    assert valve_only["signals"]["technical_branch_available"] is None
    assert valve_only["signals"]["technical_metadata_incomplete"] is None
    assert "technical" not in valve_only["source_errors"]
    assert mirror_calls == 1
