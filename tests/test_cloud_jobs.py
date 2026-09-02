from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any

import pytest

from steam_mcp.cache import TtlLruCache
from steam_mcp.cloud_jobs import (
    CloudTasksJobRunner,
    FirestoreJobStore,
    GcsResultStore,
    WorkerEndpoint,
)
from steam_mcp.contracts import CooperativeCancellation, ErrorCode, ServiceError
from steam_mcp.cursor import CursorCodec
from steam_mcp.jobs import MemoryJobStore, MemoryResultStore
from steam_mcp.services.analysis import AnalysisService


def run(value: Any) -> Any:
    return asyncio.run(value)


class Snapshot:
    def __init__(self, value: dict[str, Any] | None) -> None:
        self.value = dict(value) if value is not None else None
        self.exists = value is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self.value) if self.value is not None else None


class FakeDocument:
    def __init__(self, client: FakeFirestore, job_id: str) -> None:
        self.client = client
        self.job_id = job_id

    async def get(self, transaction: Any = None) -> Snapshot:
        del transaction
        return Snapshot(self.client.values.get(self.job_id))

    async def set(self, value: dict[str, Any]) -> None:
        self.client.values[self.job_id] = dict(value)

    async def update(self, value: dict[str, Any]) -> None:
        self.client.values[self.job_id].update(value)


class FakeCollection:
    def __init__(self, client: FakeFirestore) -> None:
        self.client = client

    def document(self, job_id: str) -> FakeDocument:
        return FakeDocument(self.client, job_id)


class FakeTransaction:
    async def set(self, document: FakeDocument, value: dict[str, Any]) -> None:
        await document.set(value)

    async def update(self, document: FakeDocument, value: dict[str, Any]) -> None:
        await document.update(value)


class FakeFirestore:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()

    def collection(self, name: str) -> FakeCollection:
        assert name == "steam_jobs"
        return FakeCollection(self)

    async def run_transaction(self, callback: Any) -> Any:
        async with self.lock:
            return await callback(FakeTransaction())


def test_firestore_transactional_idempotency_shape_ttl_and_claim() -> None:
    client = FakeFirestore()
    store = FirestoreJobStore(client=client, ttl_seconds=604_800)
    request_id = "punctuation / 한글 ?"
    job = run(store.create("game_overview", ["10"], {"language": "ko"}, request_id))
    expected = "request-" + hashlib.sha256(request_id.encode()).hexdigest()
    assert job.job_id == expected
    stored = client.values[expected]
    assert set(stored) == {
        "task",
        "input_hash",
        "status",
        "progress",
        "cancel_requested",
        "error",
        "result_object_path",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert not ({"refs", "options", "request_id", "job_id"} & set(stored))
    assert isinstance(stored["expires_at"], datetime)
    assert int((stored["expires_at"] - stored["created_at"]).total_seconds()) == 604_800
    assert run(store.create("game_overview", ["10"], {"language": "ko"}, request_id)).job_id == expected
    with pytest.raises(ServiceError) as exc:
        run(store.create("game_overview", ["11"], {"language": "ko"}, request_id))
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT

    async def claim_twice() -> list[Any]:
        return await asyncio.gather(
            store.claim(job.job_id, attempt=0),
            store.claim(job.job_id, attempt=0),
        )

    claims = run(claim_twice())
    assert sum(value is not None for value in claims) == 1
    assert client.values[job.job_id]["status"] == "running"


def test_firestore_concurrent_conflicting_request_cannot_overwrite() -> None:
    client = FakeFirestore()
    store = FirestoreJobStore(client=client)

    async def race() -> list[Any]:
        return await asyncio.gather(
            store.create("game_overview", ["10"], {}, "same"),
            store.create("game_overview", ["11"], {}, "same"),
            return_exceptions=True,
        )

    results = run(race())
    assert sum(not isinstance(value, Exception) for value in results) == 1
    errors = [value for value in results if isinstance(value, ServiceError)]
    assert len(errors) == 1 and errors[0].code is ErrorCode.INVALID_ARGUMENT
    stored = next(iter(client.values.values()))
    assert stored["input_hash"] in {
        hashlib.sha256(
            json.dumps(
                {"task": "game_overview", "refs": [ref], "options": {}},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for ref in ("10", "11")
    }


class FakeBlob:
    def __init__(self, path: str) -> None:
        self.path = path
        self.body = b""
        self.content_type = ""
        self.content_encoding: str | None = None
        self.raw_download = False

    def upload_from_string(self, body: bytes, *, content_type: str) -> None:
        self.body = body
        self.content_type = content_type

    def download_as_bytes(self, *, raw_download: bool = False) -> bytes:
        self.raw_download = raw_download
        return self.body


class FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}

    def blob(self, path: str) -> FakeBlob:
        return self.blobs.setdefault(path, FakeBlob(path))


class FakeStorage:
    def __init__(self) -> None:
        self.bucket_value = FakeBucket()

    def bucket(self, name: str) -> FakeBucket:
        assert name == "private-results"
        return self.bucket_value


def test_gcs_result_exact_gzip_jsonl_path_and_roundtrip() -> None:
    client = FakeStorage()
    store = GcsResultStore("private-results", client=client)
    value = {"items": [{"한글": "결과"}]}
    ref = run(store.put("job-1", value))
    assert ref == "gs://private-results/steam-jobs/job-1/result.jsonl.gz"
    blob = client.bucket_value.blobs["steam-jobs/job-1/result.jsonl.gz"]
    assert blob.content_encoding == "gzip"
    assert blob.content_type == "application/x-ndjson"
    assert json.loads(gzip.decompress(blob.body).decode().strip()) == value
    assert run(store.get(ref)) == value
    assert blob.raw_download is True


class FakeTasks:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    async def create_task(self, **request: Any) -> None:
        self.created.append(request)


def test_cloud_tasks_payload_path_oidc_and_no_firestore_inputs() -> None:
    client = FakeTasks()
    runner = CloudTasksJobRunner(
        project="p",
        location="asia-northeast1",
        queue="steam-analysis",
        worker_url="https://worker/internal/jobs/run",
        service_account_email="tasks@example.iam.gserviceaccount.com",
        worker_token="defense",
        client=client,
    )
    run(runner.submit("job", {"task": "game_overview", "refs": ["10"], "options": {}}))
    request = client.created[0]["request"]
    assert request["parent"].endswith("/queues/steam-analysis")
    task = request["task"]
    assert task["name"].endswith("/queues/steam-analysis/tasks/job")
    http = task["http_request"]
    assert http["url"].endswith("/internal/jobs/run")
    assert http["oidc_token"]["service_account_email"].startswith("tasks@")
    assert json.loads(http["body"])["payload"]["refs"] == ["10"]


def test_cloud_tasks_duplicate_name_is_idempotent() -> None:
    class DuplicateTasks(FakeTasks):
        async def create_task(self, **request: Any) -> None:
            self.created.append(request)
            raise type("AlreadyExists", (Exception,), {})()

    client = DuplicateTasks()
    runner = CloudTasksJobRunner(
        project="p",
        location="asia-northeast1",
        queue="steam-analysis",
        worker_url="https://worker/internal/jobs/run",
        client=client,
    )
    run(runner.submit("same-job", {"task": "game_overview", "refs": ["10"]}))
    assert client.created[0]["request"]["task"]["name"].endswith("/tasks/same-job")


def test_cloud_tasks_client_is_created_lazily_on_submit_loop() -> None:
    client = FakeTasks()
    factory_calls: list[bool] = []

    def factory() -> FakeTasks:
        factory_calls.append(True)
        return client

    runner = CloudTasksJobRunner(
        project="p",
        location="asia-northeast1",
        queue="steam-analysis",
        worker_url="https://worker/internal/jobs/run",
        client_factory=factory,
    )
    assert factory_calls == []
    run(runner.submit("lazy-job", {"task": "game_overview", "refs": ["10"]}))
    assert factory_calls == [True]
    assert client.created[0]["request"]["parent"].endswith("/queues/steam-analysis")


async def endpoint_request(
    endpoint: WorkerEndpoint,
    payload: dict[str, Any],
    *,
    token: str = "",
    retry: int = 0,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = [(b"x-cloudtasks-taskretrycount", str(retry).encode())]
    if token:
        headers.append((b"x-steam-worker-token", token.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/internal/jobs/run",
        "headers": headers,
    }
    sent: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await endpoint(scope, receive, send)
    return sent[0]["status"], json.loads(sent[1]["body"])


def test_worker_endpoint_auth_and_retry_header() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def handler(job_id: str, payload: dict[str, Any]) -> None:
        seen.append((job_id, payload))

    endpoint = WorkerEndpoint(handler, token="secret")
    status, _ = run(endpoint_request(endpoint, {"job_id": "j", "payload": {}}, token="wrong"))
    assert status == 401
    status, _ = run(
        endpoint_request(endpoint, {"job_id": "j", "payload": {}}, token="secret", retry=2)
    )
    assert status == 200
    assert seen == [("j", {"_attempt": 2})]


class RetryRunner:
    supports_retry = True

    async def submit(self, job_id: str, payload: dict[str, Any]) -> None:
        del job_id, payload


class FailingSubmitRunner:
    supports_retry = True

    async def submit(self, job_id: str, payload: dict[str, Any]) -> None:
        del job_id, payload
        raise RuntimeError("queue unavailable")


def test_analysis_submit_failure_marks_job_failed_and_returns_job_id() -> None:
    store = MemoryJobStore()
    service = AnalysisService(
        RetryBackend(fail=False),
        TtlLruCache(),
        CursorCodec(b"q" * 32),
        store,
        MemoryResultStore(),
        FailingSubmitRunner(),
    )
    with pytest.raises(ServiceError) as caught:
        run(service.start("game_overview", ["10"], {}, "queue-failure"))
    job_id = caught.value.details["job_id"]
    job = run(store.get(job_id))
    assert caught.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert job is not None and job.status == "failed"
    assert job.progress["stage"] == "queue_failed"


class RetryBackend:
    def __init__(self, *, fail: bool = True) -> None:
        self.fail = fail
        self.calls = 0

    async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
        del operation, arguments
        self.calls += 1
        if self.fail:
            raise ServiceError(ErrorCode.PROVIDER_UNAVAILABLE, "temporary", retryable=True)
        return {"ok": True}


def analysis_service(backend: Any, store: MemoryJobStore) -> AnalysisService:
    return AnalysisService(
        backend,
        TtlLruCache(),
        CursorCodec(b"j" * 32),
        store,
        MemoryResultStore(),
        RetryRunner(),
    )


def test_worker_retries_twice_then_marks_third_attempt_failed() -> None:
    store = MemoryJobStore()
    backend = RetryBackend()
    service = analysis_service(backend, store)
    job = run(store.create("purchase_decision", ["10"], {}))
    endpoint = WorkerEndpoint(service.run_job)
    request = {
        "job_id": job.job_id,
        "payload": {"task": "purchase_decision", "refs": ["10"], "options": {}},
    }
    assert run(endpoint_request(endpoint, request, retry=0))[0] == 503
    assert run(store.get(job.job_id)).status == "queued"
    assert run(endpoint_request(endpoint, request, retry=1))[0] == 503
    assert run(endpoint_request(endpoint, request, retry=2))[0] == 200
    assert run(store.get(job.job_id)).status == "failed"
    assert backend.calls == 3


def test_duplicate_terminal_delivery_is_noop_and_cancel_checks_fanout_boundary() -> None:
    store = MemoryJobStore()
    backend = RetryBackend(fail=False)
    service = analysis_service(backend, store)
    job = run(store.create("purchase_decision", ["10"], {}))
    payload = {"task": "purchase_decision", "refs": ["10"], "options": {}}
    run(service.run_job(job.job_id, payload))
    run(service.run_job(job.job_id, payload))
    assert backend.calls == 1
    assert run(store.get(job.job_id)).status == "succeeded"

    cancelling_store = MemoryJobStore()

    class CancellingBackend:
        def __init__(self) -> None:
            self.calls = 0
            self.job_id = ""

        async def call(self, operation: str, arguments: dict[str, Any]) -> Any:
            del operation, arguments
            self.calls += 1
            await cancelling_store.request_cancel(self.job_id)
            return {"ok": True}

    cancelling = CancellingBackend()
    cancelling_service = analysis_service(cancelling, cancelling_store)
    cancel_job = run(cancelling_store.create("game_overview", ["10"], {}))
    cancelling.job_id = cancel_job.job_id
    run(
        cancelling_service.run_job(
            cancel_job.job_id,
            {"task": "game_overview", "refs": ["10"], "options": {}},
        )
    )
    assert cancelling.calls == 1
    cancelled = run(cancelling_store.get(cancel_job.job_id))
    assert cancelled.status == "cancelled"
    assert cancelled.progress == {"stage": "cancelled"}
    assert cancelled.error is None


def test_cooperative_cancellation_is_not_converted_to_a_provider_error() -> None:
    import steam_mcp.legacy_backend as legacy

    with pytest.raises(CooperativeCancellation):
        legacy._handle_error(CooperativeCancellation())


@pytest.mark.skipif(
    not os.getenv("FIRESTORE_EMULATOR_HOST"),
    reason="requires the Firestore emulator",
)
def test_firestore_emulator_idempotency_transition_and_restart_recovery() -> None:
    from google.cloud.firestore_v1.async_client import AsyncClient

    async def exercise() -> None:
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "demo-steam-mcp")
        collection = f"steam_jobs_test_{uuid.uuid4().hex}"
        client = AsyncClient(project=project)
        first = FirestoreJobStore(client=client, collection=collection, ttl_seconds=604_800)
        job = await first.create("game_overview", ["10"], {"language": "ko"}, "emulator")
        snapshot = await client.collection(collection).document(job.job_id).get()
        assert not ({"refs", "options", "request_id", "job_id"} & set(snapshot.to_dict()))

        restarted = FirestoreJobStore(client=client, collection=collection, ttl_seconds=604_800)
        recovered = await restarted.get(job.job_id)
        assert recovered is not None and recovered.status == "queued"
        assert (await restarted.create(
            "game_overview", ["10"], {"language": "ko"}, "emulator"
        )).job_id == job.job_id
        with pytest.raises(ServiceError) as exc:
            await restarted.create("game_overview", ["11"], {}, "emulator")
        assert exc.value.code is ErrorCode.INVALID_ARGUMENT

        claimed = await restarted.claim(job.job_id, attempt=0)
        assert claimed is not None and claimed.status == "running"
        assert await restarted.claim(job.job_id, attempt=0) is None
        await restarted.update(
            job.job_id,
            status="succeeded",
            progress={"stage": "complete", "percent": 100},
            result_ref="gs://bucket/steam-jobs/job/result.jsonl.gz",
        )
        final = await restarted.get(job.job_id)
        assert final is not None and final.status == "succeeded"
        assert final.result_ref == "gs://bucket/steam-jobs/job/result.jsonl.gz"
        await client.collection(collection).document(job.job_id).delete()
        client.close()

    run(exercise())
