"""Job and result-store interfaces plus the local 24-hour implementation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .contracts import ErrorCode, ServiceError


TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class JobRecord:
    job_id: str
    task: str
    refs: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    request_id: str | None = None
    input_hash: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result_ref: str | None = None
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 86_400)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JobRecord:
        allowed = cls.__dataclass_fields__
        fields = {key: val for key, val in value.items() if key in allowed}
        for key in ("created_at", "updated_at", "expires_at"):
            if hasattr(fields.get(key), "timestamp"):
                fields[key] = fields[key].timestamp()
        return cls(**fields)


class JobStore(Protocol):
    async def create(
        self,
        task: str,
        refs: list[str],
        options: dict[str, Any],
        request_id: str | None = None,
    ) -> JobRecord: ...

    async def get(self, job_id: str) -> JobRecord | None: ...

    async def update(self, job_id: str, **changes: Any) -> JobRecord: ...

    async def claim(self, job_id: str, *, attempt: int) -> JobRecord | None: ...

    async def request_cancel(self, job_id: str) -> JobRecord: ...


class ResultStore(Protocol):
    async def put(self, job_id: str, value: Any) -> str: ...

    async def get(self, result_ref: str) -> Any: ...


class JobRunner(Protocol):
    async def submit(self, job_id: str, payload: dict[str, Any]) -> None: ...


class MemoryJobStore:
    """Bounded process-local jobs with a fixed 24-hour retention window."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 86_400,
        max_jobs: int = 512,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds < 1 or max_jobs < 1:
            raise ValueError("job bounds must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_jobs = max_jobs
        self.clock = clock
        self._jobs: OrderedDict[str, JobRecord] = OrderedDict()
        self._requests: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _purge(self) -> None:
        now = self.clock()
        expired = [job_id for job_id, job in self._jobs.items() if job.expires_at <= now]
        for job_id in expired:
            job = self._jobs.pop(job_id)
            if job.request_id:
                self._requests.pop(job.request_id, None)

    async def create(
        self,
        task: str,
        refs: list[str],
        options: dict[str, Any],
        request_id: str | None = None,
    ) -> JobRecord:
        async with self._lock:
            self._purge()
            if request_id and request_id in self._requests:
                existing = self._jobs.get(self._requests[request_id])
                if existing is not None:
                    digest = hashlib.sha256(
                        json.dumps(
                            {"task": task, "refs": refs, "options": options},
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    if existing.input_hash != digest:
                        raise ServiceError(
                            ErrorCode.INVALID_ARGUMENT,
                            "request_id was already used with different analysis input.",
                        )
                    return existing
            now = self.clock()
            job = JobRecord(
                job_id=uuid.uuid4().hex,
                task=task,
                refs=list(refs),
                options=dict(options),
                request_id=request_id,
                input_hash=hashlib.sha256(
                    json.dumps(
                        {"task": task, "refs": refs, "options": options},
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                created_at=now,
                updated_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._jobs[job.job_id] = job
            if request_id:
                self._requests[request_id] = job.job_id
            while len(self._jobs) > self.max_jobs:
                _, removed = self._jobs.popitem(last=False)
                if removed.request_id:
                    self._requests.pop(removed.request_id, None)
            return job

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            self._purge()
            job = self._jobs.get(job_id)
            if job is not None:
                self._jobs.move_to_end(job_id)
            return job

    async def update(self, job_id: str, **changes: Any) -> JobRecord:
        async with self._lock:
            self._purge()
            job = self._jobs.get(job_id)
            if job is None:
                raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
            for key, value in changes.items():
                if key not in JobRecord.__dataclass_fields__ or key in {"job_id", "created_at"}:
                    raise ValueError(f"unsupported job field: {key}")
                setattr(job, key, value)
            job.updated_at = self.clock()
            self._jobs.move_to_end(job_id)
            return job

    async def claim(self, job_id: str, *, attempt: int) -> JobRecord | None:
        """Atomically claim a queued job or a newer retry of a stale run."""
        async with self._lock:
            self._purge()
            job = self._jobs.get(job_id)
            if job is None:
                raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
            prior_attempt = int(job.progress.get("attempt", 0))
            claim_attempt = max(0, attempt) + 1
            if job.status != "queued" and not (
                job.status == "running" and prior_attempt < claim_attempt
            ):
                return None
            job.status = "running"
            job.progress = {"stage": "fetching", "attempt": claim_attempt}
            job.error = None
            job.updated_at = self.clock()
            self._jobs.move_to_end(job_id)
            return job

    async def request_cancel(self, job_id: str) -> JobRecord:
        job = await self.get(job_id)
        if job is None:
            raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
        if job.status in TERMINAL_STATES:
            return job
        return await self.update(job_id, status="cancel_requested")


class MemoryResultStore:
    def __init__(self, max_results: int = 512) -> None:
        self.max_results = max_results
        self._values: OrderedDict[str, Any] = OrderedDict()
        self._lock = asyncio.Lock()

    async def put(self, job_id: str, value: Any) -> str:
        ref = f"memory://result/{job_id}"
        async with self._lock:
            self._values[ref] = value
            self._values.move_to_end(ref)
            while len(self._values) > self.max_results:
                self._values.popitem(last=False)
        return ref

    async def get(self, result_ref: str) -> Any:
        async with self._lock:
            if result_ref not in self._values:
                raise ServiceError(ErrorCode.JOB_EXPIRED, "The job result has expired.")
            self._values.move_to_end(result_ref)
            return self._values[result_ref]


class InlineJobRunner:
    """Local runner that completes within the initiating MCP request."""

    def __init__(self) -> None:
        self.supports_retry = False
        self._handler: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    def bind(self, handler: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._handler = handler

    async def submit(self, job_id: str, payload: dict[str, Any]) -> None:
        if self._handler is None:
            raise RuntimeError("InlineJobRunner is not bound to a job handler")
        await self._handler(job_id, payload)

    async def drain(self) -> None:
        return None
