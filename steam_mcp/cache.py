"""Small process-local LRU TTL cache used by bounded read services."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    expires_at: float
    value: Any


class TtlLruCache:
    def __init__(
        self,
        max_entries: int = 512,
        ttl_seconds: int = 600,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1 or ttl_seconds < 1:
            raise ValueError("cache bounds must be positive")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self.clock():
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds or self.ttl_seconds
        async with self._lock:
            self._entries[key] = _Entry(self.clock() + ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    async def get_or_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        ttl_seconds: int | None = None,
    ) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(loader())
                self._inflight[key] = task
        try:
            value = await task
            await self.set(key, value, ttl_seconds)
            return value
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._entries)
