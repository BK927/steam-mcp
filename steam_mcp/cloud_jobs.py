"""Optional Google Cloud job, result, and worker adapters.

Google packages are imported only when their concrete adapters are constructed,
so the default Steam MCP install stays small and tests can inject lightweight
fakes without Cloud credentials.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import inspect
import json
import secrets
from typing import Any

from .contracts import ErrorCode, ServiceError
from .jobs import JobRecord, TERMINAL_STATES


async def _invoke(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class FirestoreJobStore:
    def __init__(
        self,
        *,
        client: Any | None = None,
        collection: str = "steam_jobs",
        ttl_seconds: int = 86_400,
    ) -> None:
        if client is None:
            try:
                from google.cloud.firestore_v1.async_client import AsyncClient
            except ImportError as exc:  # pragma: no cover - optional install
                raise RuntimeError("Install steam-mcp[gcp] to use FirestoreJobStore") from exc
            client = AsyncClient()
        self.client = client
        self.collection_name = collection
        self.ttl_seconds = ttl_seconds
        self._transaction_lock = asyncio.Lock()

    def _doc(self, job_id: str) -> Any:
        return self.client.collection(self.collection_name).document(job_id)

    @staticmethod
    def _record(job_id: str, snapshot: Any) -> JobRecord | None:
        exists = getattr(snapshot, "exists", True)
        if not exists:
            return None
        value = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if not value:
            return None
        fields = dict(value)
        fields["job_id"] = job_id
        fields["result_ref"] = fields.pop("result_object_path", None)
        return JobRecord.from_dict(fields)

    async def _transaction(self, callback: Any) -> Any:
        """Run callback in a Firestore transaction or an injectable fake."""
        custom = getattr(self.client, "run_transaction", None)
        if custom is not None:
            return await _invoke(custom, callback)
        try:
            from google.cloud.firestore_v1.async_transaction import async_transactional
        except ImportError:
            # Unit-test fakes may intentionally omit Google packages. The lock
            # preserves equivalent process-local atomicity for those adapters.
            async with self._transaction_lock:
                return await callback(None)
        transaction = self.client.transaction()
        return await async_transactional(callback)(transaction)

    @staticmethod
    async def _transaction_get(transaction: Any, document: Any) -> Any:
        if transaction is None:
            return await _invoke(document.get)
        return await _invoke(document.get, transaction=transaction)

    @staticmethod
    async def _transaction_set(
        transaction: Any, document: Any, value: dict[str, Any]
    ) -> None:
        if transaction is None:
            await _invoke(document.set, value)
            return
        await _invoke(transaction.set, document, value)

    @staticmethod
    async def _transaction_update(
        transaction: Any, document: Any, value: dict[str, Any]
    ) -> None:
        if transaction is None:
            await _invoke(document.update, value)
            return
        await _invoke(transaction.update, document, value)

    async def create(
        self,
        task: str,
        refs: list[str],
        options: dict[str, Any],
        request_id: str | None = None,
    ) -> JobRecord:
        from datetime import datetime, timedelta, timezone
        import time
        import uuid

        input_body = json.dumps(
            {"task": task, "refs": refs, "options": options},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        input_hash = hashlib.sha256(input_body).hexdigest()
        # A deterministic request document makes every request_id idempotent,
        # including punctuation and non-ASCII input.
        job_id = (
            f"request-{hashlib.sha256(request_id.encode()).hexdigest()}"
            if request_id
            else uuid.uuid4().hex
        )
        now = time.time()
        job = JobRecord(
            job_id=job_id,
            task=task,
            refs=list(refs),
            options=dict(options),
            request_id=request_id,
            input_hash=input_hash,
            created_at=now,
            updated_at=now,
            expires_at=now + self.ttl_seconds,
        )
        timestamp = datetime.now(timezone.utc)
        stored = {
            "task": task,
            "input_hash": input_hash,
            "status": job.status,
            "progress": job.progress,
            "cancel_requested": False,
            "error": None,
            "result_object_path": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "expires_at": timestamp + timedelta(seconds=self.ttl_seconds),
        }
        document = self._doc(job_id)

        async def create_if_absent(transaction: Any) -> JobRecord:
            snapshot = await self._transaction_get(transaction, document)
            existing = self._record(job_id, snapshot)
            if existing is not None:
                if existing.input_hash != input_hash:
                    raise ServiceError(
                        ErrorCode.INVALID_ARGUMENT,
                        "request_id was already used with different analysis input.",
                    )
                return existing
            await self._transaction_set(transaction, document, stored)
            return job

        created = await self._transaction(create_if_absent)
        # refs/options travel only in the authenticated Cloud Task payload and
        # are deliberately absent from the Firestore status document.
        return created

    async def get(self, job_id: str) -> JobRecord | None:
        snap = await _invoke(self._doc(job_id).get)
        return self._record(job_id, snap)

    async def update(self, job_id: str, **changes: Any) -> JobRecord:
        from datetime import datetime, timezone

        if await self.get(job_id) is None:
            raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
        stored_changes = dict(changes)
        if "result_ref" in stored_changes:
            stored_changes["result_object_path"] = stored_changes.pop("result_ref")
        stored_changes["updated_at"] = datetime.now(timezone.utc)
        await _invoke(self._doc(job_id).update, stored_changes)
        job = await self.get(job_id)
        assert job is not None
        return job

    async def claim(self, job_id: str, *, attempt: int) -> JobRecord | None:
        from datetime import datetime, timezone

        document = self._doc(job_id)
        claim_attempt = max(0, attempt) + 1

        async def claim_if_available(transaction: Any) -> JobRecord | None:
            snapshot = await self._transaction_get(transaction, document)
            current = self._record(job_id, snapshot)
            if current is None:
                raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
            prior_attempt = int(current.progress.get("attempt", 0))
            if current.status != "queued" and not (
                current.status == "running" and prior_attempt < claim_attempt
            ):
                return None
            updates = {
                "status": "running",
                "progress": {"stage": "fetching", "attempt": claim_attempt},
                "error": None,
                "updated_at": datetime.now(timezone.utc),
            }
            await self._transaction_update(transaction, document, updates)
            current.status = "running"
            current.progress = updates["progress"]
            current.error = None
            current.updated_at = updates["updated_at"].timestamp()
            return current

        return await self._transaction(claim_if_available)

    async def request_cancel(self, job_id: str) -> JobRecord:
        job = await self.get(job_id)
        if job is None:
            raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
        if job.status in TERMINAL_STATES:
            return job
        return await self.update(job_id, status="cancel_requested", cancel_requested=True)


class GcsResultStore:
    def __init__(
        self,
        bucket: str,
        *,
        client: Any | None = None,
        prefix: str = "steam-jobs",
    ) -> None:
        if client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:  # pragma: no cover - optional install
                raise RuntimeError("Install steam-mcp[gcp] to use GcsResultStore") from exc
            client = storage.Client()
        self.client = client
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")

    def _blob(self, job_id: str) -> Any:
        return self.client.bucket(self.bucket_name).blob(
            f"{self.prefix}/{job_id}/result.jsonl.gz"
        )

    async def put(self, job_id: str, value: Any) -> str:
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        body = gzip.compress(line.encode("utf-8"))
        blob = self._blob(job_id)
        blob.content_encoding = "gzip"
        await asyncio.to_thread(
            blob.upload_from_string,
            body,
            content_type="application/x-ndjson",
        )
        return f"gs://{self.bucket_name}/{self.prefix}/{job_id}/result.jsonl.gz"

    async def get(self, result_ref: str) -> Any:
        prefix = f"gs://{self.bucket_name}/{self.prefix}/"
        suffix = "/result.jsonl.gz"
        if not result_ref.startswith(prefix) or not result_ref.endswith(suffix):
            raise ServiceError(ErrorCode.JOB_EXPIRED, "The result reference is invalid.")
        job_id = result_ref[len(prefix):-len(suffix)]
        raw = await asyncio.to_thread(self._blob(job_id).download_as_bytes)
        rows = [
            json.loads(line)
            for line in gzip.decompress(raw).decode("utf-8").splitlines()
            if line.strip()
        ]
        return rows[0] if len(rows) == 1 else rows


class CloudTasksJobRunner:
    def __init__(
        self,
        *,
        project: str,
        location: str,
        queue: str,
        worker_url: str,
        service_account_email: str | None = None,
        worker_token: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.supports_retry = True
        if client is None:
            try:
                from google.cloud import tasks_v2
            except ImportError as exc:  # pragma: no cover - optional install
                raise RuntimeError("Install steam-mcp[gcp] to use CloudTasksJobRunner") from exc
            client = tasks_v2.CloudTasksAsyncClient()
        self.client = client
        self.parent = client.queue_path(project, location, queue)
        self.worker_url = worker_url
        self.service_account_email = service_account_email
        self.worker_token = worker_token

    async def submit(self, job_id: str, payload: dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json"}
        if self.worker_token:
            headers["X-Steam-Worker-Token"] = self.worker_token
        request: dict[str, Any] = {
            "http_method": "POST",
            "url": self.worker_url,
            "headers": headers,
            "body": json.dumps(
                {"job_id": job_id, "payload": payload}, separators=(",", ":")
            ).encode(),
        }
        if self.service_account_email:
            request["oidc_token"] = {
                "service_account_email": self.service_account_email,
                "audience": self.worker_url,
            }
        await _invoke(self.client.create_task, parent=self.parent, task={"http_request": request})


class WorkerEndpoint:
    """Minimal injectable ASGI endpoint used by Cloud Tasks."""

    def __init__(
        self,
        handler: Any,
        *,
        path: str = "/internal/jobs/run",
        token: str | None = None,
        max_body_bytes: int = 16_384,
    ) -> None:
        self.handler = handler
        self.path = path
        self.token = token
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.path:
            await self._respond(send, 404, {"error": "not_found"})
            return
        if scope.get("method", "").upper() != "POST":
            await self._respond(send, 405, {"error": "method_not_allowed"})
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if self.token:
            supplied = headers.get(b"x-steam-worker-token", b"").decode(errors="ignore")
            if not secrets.compare_digest(supplied, self.token):
                await self._respond(send, 401, {"error": "unauthorized"})
                return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                await self._respond(send, 413, {"error": "body_too_large"})
                return
            more = bool(message.get("more_body"))
        try:
            payload = json.loads(body or b"{}")
            job_id = str(payload["job_id"])
            job_payload = payload.get("payload") or {}
            if not isinstance(job_payload, dict):
                raise TypeError("payload")
            retry_count = headers.get(b"x-cloudtasks-taskretrycount", b"0")
            job_payload["_attempt"] = int(retry_count.decode(errors="ignore") or "0")
        except (KeyError, TypeError, ValueError):
            await self._respond(send, 400, {"error": "invalid_job"})
            return
        try:
            await self.handler(job_id, job_payload)
        except Exception:  # noqa: BLE001 - failures must reach Cloud Tasks as 5xx
            await self._respond(send, 503, {"error": "retryable_job_failure", "job_id": job_id})
            return
        await self._respond(send, 200, {"ok": True, "job_id": job_id})

    @staticmethod
    async def _respond(send: Any, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
