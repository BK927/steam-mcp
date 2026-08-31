"""Normalize current Steam app-info snapshots from the SteamCMD API mirror.

The mirror exposes the same public ``app_info_print`` tree produced by SteamCMD.
This module is intentionally network-agnostic: ``steam_mcp.server`` owns fetching,
host allowlisting, retries, and caching, while these helpers turn loosely typed VDF
JSON into compact, stable MCP response shapes.

Only current state is represented. No function here persists data or claims to
provide historical prices, player counts, builds, or app-info revisions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_items(value: Any):
    if isinstance(value, dict):
        return value.items()
    if isinstance(value, list):
        return ((str(index), item) for index, item in enumerate(value))
    return ()


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").split(",")
    return [str(part).strip() for part in parts if str(part).strip()]


def _iso_utc(value: Any) -> Optional[str]:
    timestamp = _int_or_none(value)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return (
            datetime.fromtimestamp(timestamp, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OSError, OverflowError, ValueError):
        return None


def extract_app_info(payload: Any, appid: int) -> dict[str, Any]:
    """Extract one app-info object or raise a concise ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("SteamCMD API returned a non-object response")
    if payload.get("status") != "success":
        detail = payload.get("data") or payload.get("message") or "unknown error"
        raise ValueError(f"SteamCMD API could not return app {appid}: {detail}")
    data = _as_dict(payload.get("data"))
    app = data.get(str(appid))
    if not isinstance(app, dict) or not app:
        raise ValueError(f"SteamCMD API response did not contain app {appid}")
    return app


def normalize_associations(app: dict[str, Any]) -> list[dict[str, str]]:
    common = _as_dict(app.get("common"))
    rows = []
    for _, raw in _as_items(common.get("associations")):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("type") or "").strip()
        if name or kind:
            rows.append({"name": name, "type": kind})
    return rows


def normalize_launch_options(app: dict[str, Any]) -> list[dict[str, Any]]:
    config = _as_dict(app.get("config"))
    rows = []
    for key, raw in _as_items(config.get("launch")):
        if not isinstance(raw, dict):
            continue
        conditions = _as_dict(raw.get("config"))
        rows.append(
            {
                "id": str(key),
                "description": raw.get("description"),
                "executable": raw.get("executable"),
                "arguments": raw.get("arguments"),
                "type": raw.get("type"),
                "os": _split_csv(conditions.get("oslist") or raw.get("oslist")),
                "arch": conditions.get("osarch") or raw.get("osarch"),
                "language": conditions.get("language") or raw.get("language"),
                "branch": conditions.get("betakey") or raw.get("betakey"),
            }
        )
    return rows


def normalize_branches(app: dict[str, Any]) -> list[dict[str, Any]]:
    depots = _as_dict(app.get("depots"))
    branches = _as_dict(depots.get("branches"))
    rows = []
    for name, raw in branches.items():
        if not isinstance(raw, dict):
            continue
        updated = _int_or_none(raw.get("timeupdated"))
        build_updated = _int_or_none(raw.get("timebuildupdated"))
        rows.append(
            {
                "name": str(name),
                "is_public": str(name) == "public",
                "build_id": (
                    str(raw.get("buildid"))
                    if raw.get("buildid") not in (None, "")
                    else None
                ),
                "description": str(raw.get("description") or "").strip() or None,
                "password_required": _truthy(raw.get("pwdrequired")),
                "time_updated": updated,
                "updated_at": _iso_utc(updated),
                "time_build_updated": build_updated,
                "build_updated_at": _iso_utc(build_updated),
            }
        )
    rows.sort(key=lambda row: (not row["is_public"], row["name"].casefold()))
    return rows


def normalize_manifest(value: Any) -> Optional[dict[str, Any]]:
    """Normalize both old string GIDs and newer {gid,size,download} records."""
    if isinstance(value, dict):
        gid = value.get("gid") or value.get("manifest")
        size = _int_or_none(value.get("size"))
        download = _int_or_none(value.get("download"))
    else:
        gid = value
        size = None
        download = None
    if gid in (None, ""):
        return None
    return {
        "gid": str(gid),
        "size_bytes": size,
        "download_bytes": download,
    }


def normalize_depots(
    app: dict[str, Any],
    *,
    branch: str = "public",
    include_all_manifests: bool = False,
) -> list[dict[str, Any]]:
    depots = _as_dict(app.get("depots"))
    rows = []
    for depot_key, raw in depots.items():
        depot_text = str(depot_key)
        if not depot_text.isdigit() or not isinstance(raw, dict):
            continue
        config = _as_dict(raw.get("config"))
        manifest_map = {}
        for branch_name, manifest_raw in _as_dict(raw.get("manifests")).items():
            manifest = normalize_manifest(manifest_raw)
            if manifest is not None:
                manifest_map[str(branch_name)] = manifest
        row = {
            "depot_id": int(depot_text),
            "name": str(raw.get("name") or "").strip() or None,
            "os": _split_csv(config.get("oslist")),
            "arch": config.get("osarch"),
            "language": config.get("language"),
            "low_violence": _truthy(config.get("lowviolence")),
            "shared_install": _truthy(raw.get("sharedinstall")),
            "depot_from_app": _int_or_none(raw.get("depotfromapp")),
            "max_size_bytes": _int_or_none(raw.get("maxsize")),
            "manifest_branches": sorted(manifest_map, key=str.casefold),
            "selected_branch": branch,
            "selected_manifest": manifest_map.get(branch),
            # Encrypted values are deliberately not exposed; only their presence is.
            "has_encrypted_manifests": bool(_as_dict(raw.get("encryptedmanifests"))),
        }
        if include_all_manifests:
            row["manifests"] = manifest_map
        rows.append(row)
    rows.sort(key=lambda row: row["depot_id"])
    return rows


def depot_matches_platform(depot: dict[str, Any], platform: str) -> bool:
    if platform == "all":
        return True
    os_values = {str(value).lower() for value in depot.get("os") or []}
    # Untagged depots are commonly shared data needed by every platform.
    return not os_values or platform.lower() in os_values


def normalize_build_snapshot(
    app: dict[str, Any],
    *,
    appid: int,
    branch: str = "public",
    platform: str = "all",
) -> dict[str, Any]:
    branch_rows = normalize_branches(app)
    branch_info = next((row for row in branch_rows if row["name"] == branch), None)
    depots = [
        depot
        for depot in normalize_depots(app, branch=branch)
        if depot_matches_platform(depot, platform)
    ]
    manifests = []
    for depot in depots:
        manifest = depot.get("selected_manifest")
        if not manifest:
            continue
        manifests.append(
            {
                "depot_id": depot["depot_id"],
                "depot_name": depot.get("name"),
                "os": depot.get("os"),
                "arch": depot.get("arch"),
                "language": depot.get("language"),
                **manifest,
            }
        )
    known_size = [
        row["size_bytes"] for row in manifests if row["size_bytes"] is not None
    ]
    known_download = [
        row["download_bytes"] for row in manifests if row["download_bytes"] is not None
    ]
    return {
        "appid": appid,
        "branch": branch,
        "platform": platform,
        "available": branch_info is not None or bool(manifests),
        "branch_info": branch_info,
        "build_id": branch_info.get("build_id") if branch_info else None,
        "updated_at": branch_info.get("updated_at") if branch_info else None,
        "build_updated_at": (
            branch_info.get("build_updated_at") if branch_info else None
        ),
        "password_required": (
            branch_info.get("password_required") if branch_info else False
        ),
        "manifest_count": len(manifests),
        "depots_considered": len(depots),
        "depots_without_selected_manifest": len(depots) - len(manifests),
        "reported_manifest_size_bytes": sum(known_size) if known_size else None,
        "reported_download_bytes": sum(known_download) if known_download else None,
        "manifests": manifests,
        "history_available": False,
    }


def normalize_common(app: dict[str, Any]) -> dict[str, Any]:
    common = _as_dict(app.get("common"))
    extended = _as_dict(app.get("extended"))
    languages = [
        str(name)
        for name, enabled in _as_dict(common.get("languages")).items()
        if _truthy(enabled)
    ]
    categories = []
    for key, enabled in _as_dict(common.get("category")).items():
        if not _truthy(enabled):
            continue
        suffix = str(key).removeprefix("category_")
        category_id = _int_or_none(suffix)
        if category_id is not None:
            categories.append(category_id)
    genres = []
    for _, genre in _as_items(common.get("genres")):
        value = _int_or_none(genre)
        if value is not None:
            genres.append(value)
    steam_release = _int_or_none(common.get("steam_release_date"))
    original_release = _int_or_none(common.get("original_release_date"))
    return {
        "name": common.get("name"),
        "type": common.get("type"),
        "release_state": common.get("releasestate"),
        "os": _split_csv(common.get("oslist")),
        "languages": sorted(languages, key=str.casefold),
        "controller_support": common.get("controller_support"),
        "community_visible_stats": _truthy(common.get("community_visible_stats")),
        "associations": normalize_associations(app),
        "genre_ids": genres,
        "category_ids": sorted(categories),
        "primary_genre_id": _int_or_none(common.get("primary_genre")),
        "review_score": _int_or_none(common.get("review_score")),
        "review_percentage": _int_or_none(common.get("review_percentage")),
        "review_count": _int_or_none(common.get("review_count")),
        "steam_release_timestamp": steam_release,
        "steam_release_at": _iso_utc(steam_release),
        "original_release_timestamp": original_release,
        "original_release_at": _iso_utc(original_release),
        "developer": extended.get("developer"),
        "publisher": extended.get("publisher"),
    }


def normalize_product_overview(
    app: dict[str, Any],
    *,
    appid: int,
    branch: str = "public",
    platform: str = "all",
    include_launch_options: bool = False,
) -> dict[str, Any]:
    branches = normalize_branches(app)
    depots = normalize_depots(app, branch=branch)
    launch_options = normalize_launch_options(app)
    config = _as_dict(app.get("config"))
    depot_root = _as_dict(app.get("depots"))
    build = normalize_build_snapshot(app, appid=appid, branch=branch, platform=platform)
    selected_branch = {
        key: value for key, value in build.items() if key not in {"manifests", "appid"}
    }
    result = {
        "appid": appid,
        "change_number": _int_or_none(app.get("_change_number")),
        "appinfo_sha": app.get("_sha"),
        "payload_size_bytes": _int_or_none(app.get("_size")),
        "missing_access_token": _truthy(app.get("_missing_token")),
        "complete_for_public_appinfo": not _truthy(app.get("_missing_token")),
        "history_available": False,
        "common": normalize_common(app),
        "configuration": {
            "install_dir": config.get("installdir"),
            "content_type": _int_or_none(config.get("contenttype")),
            "base_languages": _split_csv(depot_root.get("baselanguages")),
            "cloud_saves_configured": bool(_as_dict(app.get("ufs"))),
        },
        "counts": {
            "branches": len(branches),
            "depots": len(depots),
            "launch_options": len(launch_options),
            "password_required_branches": sum(
                1 for row in branches if row["password_required"]
            ),
        },
        "selected_branch": selected_branch,
    }
    if include_launch_options:
        result["launch_options"] = launch_options
    return result
