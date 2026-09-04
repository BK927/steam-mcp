"""Bound collection pages without advancing past undisclosed items."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any

from .cache import TtlLruCache
from .contracts import ErrorCode, ServiceError, compact_size, enforce_envelope_budget
from .cursor import CursorCodec
from .jobs import ResultStore

PREFIX = "buffer:"
MAX_SNAPSHOT_BYTES = 512 * 1024


class ResponsePager:
    def __init__(self, cache: TtlLruCache, codec: CursorCodec, max_bytes: int, store: ResultStore | None = None) -> None:
        self.cache = cache
        self.codec = codec
        self.max_bytes = max_bytes
        self.store = store

    async def run(
        self,
        operation: str,
        arguments: dict[str, Any],
        action: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        filters = {key: value for key, value in arguments.items() if key != "cursor"}
        cursor = arguments.get("cursor") or ""
        resumed = cursor.startswith(PREFIX)
        snapshot_id = uuid.uuid4().hex
        offset = 0
        expires_at = self.codec.clock() + self.codec.ttl_seconds
        result_ref = None
        if resumed:
            state = self.codec.decode(cursor[len(PREFIX):], scope=f"buffer:{operation}", filters=filters)
            snapshot_id, offset = state.get("id"), state.get("offset")
            if not isinstance(snapshot_id, str) or not isinstance(offset, int) or offset < 0:
                raise self._expired()
            snapshot = await self.cache.get(snapshot_id)
            result_ref = state.get("ref")
            if snapshot is None and self.store and isinstance(result_ref, str):
                try:
                    snapshot = await self.store.get(result_ref)
                except ServiceError as error:
                    if error.code == ErrorCode.JOB_EXPIRED:
                        raise self._expired() from error
                    raise
            if snapshot is None or snapshot["expires_at"] <= self.codec.clock():
                raise self._expired()
            source, expires_at = snapshot["payload"], snapshot["expires_at"]
            if offset >= len(source["items"]):
                raise self._expired()
        else:
            source = await action()
            job_expiry = source.get("job", {}).get("expires_at")
            if job_expiry:
                expires_at = min(expires_at, datetime.fromisoformat(job_expiry.replace("Z", "+00:00")).timestamp())
        items = source.get("items") or []
        deferred = False

        def continuation(returned: int) -> str | None:
            nonlocal deferred
            next_offset = offset + returned
            if next_offset >= len(items):
                return source["page"]["next_cursor"]
            deferred = True
            return PREFIX + self.codec.encode(
                scope=f"buffer:{operation}", filters=filters,
                state={"id": snapshot_id, "offset": next_offset, **({"ref": result_ref} if result_ref else {})},
                expires_at=expires_at,
            )

        page = copy.deepcopy(source)
        page["items"] = items[offset:]
        page["page"] = {
            "returned": len(page["items"]),
            "has_more": bool(source["page"]["next_cursor"]),
            "next_cursor": source["page"]["next_cursor"],
        }
        result = enforce_envelope_budget(page, default_bytes=self.max_bytes, continuation=continuation)
        if deferred and not resumed:
            if compact_size(source) > MAX_SNAPSHOT_BYTES:
                raise ServiceError(ErrorCode.UPSTREAM_ERROR, "The fetched page exceeds the continuation cache limit. Use a smaller limit.")
            snapshot = {"payload": copy.deepcopy(source), "expires_at": expires_at}
            if self.store:
                result_ref = await self.store.put(f"response-{snapshot_id}", snapshot)
                # The durable reference adds bytes to the signed cursor.
                result = enforce_envelope_budget(page, default_bytes=self.max_bytes, continuation=continuation)
            await self.cache.set(snapshot_id, snapshot, self.codec.ttl_seconds)
        return result

    @staticmethod
    def _expired() -> ServiceError:
        return ServiceError(ErrorCode.CURSOR_MISMATCH, "The buffered page expired or is unavailable on this server. Restart this query; no items were skipped.")
