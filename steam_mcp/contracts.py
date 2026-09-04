"""Compact public MCP contracts shared by the Steam services."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from mcp.types import CallToolResult, TextContent


SCHEMA_VERSION = "1"


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    NOT_FOUND = "NOT_FOUND"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CURSOR_MISMATCH = "CURSOR_MISMATCH"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    TIMEOUT = "TIMEOUT"
    JOB_NOT_READY = "JOB_NOT_READY"
    JOB_EXPIRED = "JOB_EXPIRED"


class ServiceError(Exception):
    """An anticipated public error with a stable machine-readable code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        schema_uri: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.schema_uri = schema_uri
        self.details = details or {}


class CooperativeCancellation(Exception):
    """Internal control flow for a user-requested analysis cancellation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def entity_envelope(
    data: Any,
    *,
    canonical_uri: str | None = None,
    provider: str | None = None,
    warnings: list[str] | None = None,
    untrusted_fields: list[str] | None = None,
) -> dict[str, Any]:
    object_data = data if isinstance(data, dict) else {"value": data}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "entity",
        "data": object_data,
        "items": [],
        "job": {},
        "page": {"returned": 0, "has_more": False, "next_cursor": None},
        "meta": _meta(
            canonical_uri=canonical_uri,
            provider=provider,
            warnings=warnings,
            untrusted_fields=untrusted_fields,
        ),
    }


def collection_envelope(
    items: list[Any],
    *,
    next_cursor: str | None = None,
    canonical_uri: str | None = None,
    provider: str | None = None,
    warnings: list[str] | None = None,
    untrusted_fields: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(items) > 100:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "A result page cannot exceed 100 items.")
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "collection",
        "data": extra or {},
        "items": items,
        "job": {},
        "page": {
            "returned": len(items),
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        },
        "meta": _meta(
            canonical_uri=canonical_uri,
            provider=provider,
            warnings=warnings,
            untrusted_fields=untrusted_fields,
        ),
    }
    return value


def job_envelope(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "job",
        "data": {},
        "items": [],
        "job": job,
        "page": {"returned": 0, "has_more": False, "next_cursor": None},
        "meta": _meta(provider="steam"),
    }


def _meta(
    *,
    canonical_uri: str | None = None,
    provider: str | None = None,
    warnings: list[str] | None = None,
    untrusted_fields: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "source": "steam",
        "provider": provider or "steam",
        "retrieved_at": utc_now(),
        "fresh_until": None,
        "quota_cost": None,
        "canonical_uri": canonical_uri,
        "warnings": warnings or [],
        "untrusted_fields": untrusted_fields or [],
    }
    return value


def success_result(
    envelope: dict[str, Any],
    summary: str,
    *,
    max_bytes: int = 12 * 1024,
) -> CallToolResult:
    """Return full structured content and only a small human-readable block."""
    envelope = enforce_envelope_budget(envelope, default_bytes=max_bytes)
    return CallToolResult(
        content=[TextContent(type="text", text=summary[:400])],
        structuredContent=envelope,
    )


def error_result(error: ServiceError) -> CallToolResult:
    payload: dict[str, Any] = {
        "code": error.code.value,
        "message": error.message,
        "retryable": error.retryable,
        "schema_uri": error.schema_uri,
        "details": error.details,
    }
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=f"{error.code.value}: {error.message}"[:400])],
        structuredContent=payload,
    )


def compact_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def bounded_value(value: Any, max_chars: int) -> tuple[Any, bool]:
    """Bound content text while preserving IDs, dates, numbers and object shape."""
    remaining = max(500, max_chars)
    truncated = False
    text_fields = {"description", "short_description", "about_the_game", "text", "review", "developer_response", "excerpt", "contents"}

    def walk(item: Any, key: str = "") -> Any:
        nonlocal remaining, truncated
        if isinstance(item, str) and key in text_fields:
            if len(item) <= remaining:
                remaining -= len(item)
                return item
            truncated = True
            kept = max(0, remaining)
            remaining = 0
            return item[:kept] + "…"
        if isinstance(item, list):
            return [walk(child, key) for child in item]
        if isinstance(item, dict):
            return {key: walk(child, key) for key, child in item.items()}
        return item

    return walk(value), truncated


def enforce_envelope_budget(
    envelope: dict[str, Any],
    *,
    default_bytes: int = 12 * 1024,
    hard_bytes: int = 32 * 1024,
    continuation: Callable[[int], str | None] | None = None,
) -> dict[str, Any]:
    """Guarantee the final compact UTF-8 envelope fits the public byte budget."""
    value = _fixed_envelope(envelope)
    default_bytes = min(default_bytes, hard_bytes)
    if compact_size(value) <= default_bytes:
        return value

    warnings = value["meta"]["warnings"]
    warnings.append("Content text was shortened to the configured byte budget; structural fields were preserved.")
    items = value["items"]
    original_item_count = len(items)
    compacted_item_fields = False
    next_cursor = value["page"]["next_cursor"]
    if items:
        per_item_chars = max(100, (default_bytes // 2) // len(items))
        reduced_items = []
        for item in items:
            reduced, was_compacted = bounded_value(item, per_item_chars)
            reduced_items.append(reduced)
            compacted_item_fields = compacted_item_fields or was_compacted
        value["items"] = reduced_items
        items = value["items"]
        value["page"]["returned"] = len(items)
        value["page"]["has_more"] = bool(next_cursor)
    if items:
        value["data"]["truncation"] = {
            "original_items": original_item_count,
            "returned_items": len(items),
            "omitted_items": original_item_count - len(items),
            "item_fields_compacted": compacted_item_fields,
        }
    truncation = value["data"].pop("truncation", None)
    value["data"], _ = bounded_value(value["data"], default_bytes // 4)
    if truncation is not None:
        value["data"]["truncation"] = truncation
    value["job"], _ = bounded_value(value["job"], default_bytes // 4)

    while len(items) > 1 and continuation and compact_size(value) > default_bytes:
        items.pop()
        next_cursor = continuation(len(items))
        value["page"]["next_cursor"] = next_cursor
        value["page"]["returned"] = len(items)
        value["page"]["has_more"] = bool(next_cursor)
        value["data"]["truncation"]["returned_items"] = len(items)
        value["data"]["truncation"]["omitted_items"] = original_item_count - len(items)
        value["data"]["truncation"]["content_omitted"] = True
        value["data"]["truncation"]["continuation_available"] = True
    if compact_size(value) > hard_bytes or compact_size(value) > default_bytes:
        raise ServiceError(
            ErrorCode.UPSTREAM_ERROR,
            "The response cannot fit the byte budget without losing structured data. Use a smaller limit or selection.",
            details={"reason": "max_result_bytes", "max_bytes": default_bytes},
        )
    return value


def _fixed_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Normalize success payloads to the exact stable object/array shape."""
    raw_meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
    meta = _meta(
        canonical_uri=raw_meta.get("canonical_uri"),
        provider=raw_meta.get("provider"),
        warnings=list(raw_meta.get("warnings") or []),
        untrusted_fields=list(raw_meta.get("untrusted_fields") or []),
    )
    for key in ("source", "retrieved_at", "fresh_until", "quota_cost"):
        if key in raw_meta:
            meta[key] = raw_meta[key]
    raw_page = envelope.get("page") if isinstance(envelope.get("page"), dict) else {}
    raw_data = envelope.get("data")
    raw_job = envelope.get("job")
    raw_items = envelope.get("items")
    items = list(raw_items) if isinstance(raw_items, list) else []
    if len(items) > 100:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "A result page cannot exceed 100 items.")
    kind = str(envelope.get("kind") or "entity")
    if kind not in {"entity", "collection", "job"}:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "Envelope kind is invalid.")
    return {
        "schema_version": str(envelope.get("schema_version") or SCHEMA_VERSION),
        "kind": kind,
        "data": copy.deepcopy(raw_data) if isinstance(raw_data, dict) else {},
        "items": copy.deepcopy(items),
        "job": copy.deepcopy(raw_job) if isinstance(raw_job, dict) else {},
        "page": {
            "returned": len(items),
            "has_more": bool(raw_page.get("has_more")),
            "next_cursor": raw_page.get("next_cursor"),
        },
        "meta": meta,
    }
