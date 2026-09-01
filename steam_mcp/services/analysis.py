"""Composite Steam analyses executed through an injectable job backend."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

from ..contracts import ErrorCode, ServiceError, bounded_value, job_envelope
from ..jobs import JobRecord, JobRunner, JobStore, ResultStore, TERMINAL_STATES
from .base import BaseService, bounded_limit, provider_checkpoint_scope

ANALYSIS_TASKS = frozenset(
    {
        "friend_ownership",
        "review_insights",
        "game_overview",
        "player_compare",
        "library_insights",
        "purchase_decision",
        "recommendations",
        "coop_plan",
    }
)


class _Cancelled(Exception):
    pass


class RetryableJobError(Exception):
    pass


class AnalysisService(BaseService):
    def __init__(
        self,
        backend: Any,
        cache: Any,
        cursor: Any,
        job_store: JobStore,
        result_store: ResultStore,
        runner: JobRunner,
    ) -> None:
        super().__init__(backend, cache, cursor)
        self.job_store = job_store
        self.result_store = result_store
        self.runner = runner
        bind = getattr(runner, "bind", None)
        if bind is not None:
            bind(self.run_job)

    async def start(
        self,
        task: str,
        refs: list[str],
        options: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        if task not in ANALYSIS_TASKS:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unsupported analysis task: {task}.",
                schema_uri=f"steam://schema/steam_analyze.{task}",
            )
        if not refs:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "refs must contain at least one reference.")
        payload = {"task": task, "refs": refs, "options": options}
        job = await self.job_store.create(task, refs, options, request_id or None)
        if job.status == "queued":
            await self.runner.submit(job.job_id, payload)
            job = await self._require_job(job.job_id)
        return job_envelope(self.public_job(job))

    async def run_job(self, job_id: str, payload: dict[str, Any]) -> None:
        job = await self._require_job(job_id)
        if job.status in TERMINAL_STATES:
            return
        if job.status == "cancel_requested":
            await self.job_store.update(job_id, status="cancelled")
            return
        attempt = max(0, int(payload.get("_attempt", 0)))
        claimed = await self.job_store.claim(job_id, attempt=attempt)
        if claimed is None:
            return
        try:
            with provider_checkpoint_scope(lambda: self._checkpoint(job_id)):
                result = await self.execute(
                    str(payload.get("task") or job.task),
                    list(payload.get("refs") or job.refs),
                    dict(payload.get("options") or job.options),
                    job_id=job_id,
                )
            await self._checkpoint(job_id)
            result_ref = await self.result_store.put(job_id, result)
            await self._checkpoint(job_id)
            await self.job_store.update(
                job_id,
                status="succeeded",
                progress={"stage": "complete", "percent": 100},
                result_ref=result_ref,
                error=None,
            )
        except _Cancelled:
            await self.job_store.update(job_id, status="cancelled")
        except ServiceError as exc:
            error = {
                "code": exc.code.value,
                "message": exc.message,
                "retryable": exc.retryable,
                "schema_uri": exc.schema_uri,
                "details": exc.details,
            }
            can_retry = bool(getattr(self.runner, "supports_retry", False)) and exc.retryable and attempt < 2
            await self.job_store.update(
                job_id,
                status="queued" if can_retry else "failed",
                progress={"stage": "retry_wait" if can_retry else "failed", "attempt": attempt + 1},
                error=error,
            )
            if can_retry:
                raise RetryableJobError(exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            can_retry = bool(getattr(self.runner, "supports_retry", False)) and attempt < 2
            await self.job_store.update(
                job_id,
                status="queued" if can_retry else "failed",
                progress={"stage": "retry_wait" if can_retry else "failed", "attempt": attempt + 1},
                error={
                    "code": ErrorCode.UPSTREAM_ERROR.value,
                    "message": "The analysis worker failed unexpectedly.",
                    "retryable": can_retry,
                    "schema_uri": None,
                    "details": {},
                },
            )
            if can_retry:
                raise RetryableJobError("analysis worker retry") from exc

    async def execute(
        self,
        task: str,
        refs: list[str],
        options: dict[str, Any],
        *,
        job_id: str | None = None,
    ) -> Any:
        if job_id:
            await self._checkpoint(job_id)
        first = refs[0]
        if task == "friend_ownership":
            self._need(refs, 2, task)
            appid = await self.appid(refs[1])
            return await self.call(
                "steam_find_friends_who_own",
                {
                    "steamid": self._user(first),
                    "appid": appid,
                    "max_friends": int(options.get("max_friends", 50)),
                    "playing_now": bool(options.get("playing_now", False)),
                    "limit": int(options.get("limit", 30)),
                },
                ttl=60,
            )
        if task == "review_insights":
            appid = await self.appid(first)
            return await self._review_insights(appid, options, job_id)
        if task == "game_overview":
            appid = await self.appid(first)
            result = {}
            calls = (
                ("store", "steam_get_app_details", {"appid": appid, "country_code": options.get("country", "us"), "language": options.get("language", "english")}),
                ("tags", "steam_get_app_tags", {"appid": appid, "limit": 20, "country_code": options.get("country", "us")}),
                ("reviews", "steam_get_app_reviews", {"appid": appid, "limit": 5, "country_code": options.get("country", "us"), "language": options.get("language", "english")}),
                ("players", "steam_get_current_players", {"appid": appid}),
                ("news", "steam_get_app_news", {"appid": appid, "count": int(options.get("news_count", 3))}),
            )
            for name, operation, arguments in calls:
                if job_id:
                    await self._checkpoint(job_id)
                result[name] = await self.call(operation, arguments, ttl=120)
            if bool(options.get("include_technical", True)):
                if job_id:
                    await self._checkpoint(job_id)
                result["technical"] = await self.call(
                    "steam_get_product_info",
                    {"appid": appid, "branch": options.get("branch", "public"), "include_launch_options": False},
                    ttl=120,
                )
            return result
        if task == "player_compare":
            self._need(refs, 2, task)
            return await self.call(
                "steam_compare_players",
                {"steamid_a": self._user(first), "steamid_b": self._user(refs[1]), "limit": int(options.get("limit", 20))},
                ttl=120,
            )
        if task == "library_insights":
            return await self.call(
                "steam_analyze_library",
                {
                    "steamid": self._user(first),
                    "top_limit": int(options.get("top_limit", 10)),
                    "backlog_limit": int(options.get("backlog_limit", 100)),
                    "abandoned_limit": int(options.get("abandoned_limit", 25)),
                    "abandoned_sort": options.get("abandoned_sort", "recent"),
                    "stale_days": int(options.get("stale_days", 365)),
                    "exclude_temp_clients": bool(options.get("exclude_temp_clients", True)),
                },
                ttl=120,
            )
        if task == "purchase_decision":
            appid = await self.appid(first)
            return await self.call(
                "steam_should_i_buy",
                {
                    "appid": appid,
                    "steamid": self._user(refs[1]) if len(refs) > 1 else options.get("player"),
                    "country_code": options.get("country", "us"),
                    "recent_max_reviews": int(options.get("recent_max_reviews", 600)),
                },
                ttl=120,
            )
        if task == "recommendations":
            seed = None if options.get("tags") else await self.appid(first)
            return await self.call(
                "steam_recommend",
                {
                    "seed_appid": seed,
                    "steamid": options.get("player"),
                    "tags": options.get("tags", []),
                    "max_price": options.get("max_price"),
                    "limit": int(options.get("limit", 10)),
                    "country_code": options.get("country", "us"),
                },
                ttl=300,
            )
        if task == "coop_plan":
            return await self.call(
                "steam_plan_coop_night",
                {
                    "steamid": self._user(first),
                    "friends": [self._user(ref) for ref in refs[1:]],
                    "mode": options.get("mode", "owned"),
                    "online_only": bool(options.get("online_only", True)),
                    "max_friends": int(options.get("max_friends", 20)),
                    "min_friends_owning": int(options.get("min_friends_owning", 1)),
                    "limit": int(options.get("limit", 20)),
                    "country_code": options.get("country", "us"),
                },
                ttl=120,
            )
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"Unsupported analysis task: {task}.")

    async def _review_insights(
        self, appid: int, options: dict[str, Any], job_id: str | None
    ) -> dict[str, Any]:
        cursor = str(options.get("cursor") or "*")
        max_reviews = max(1, min(int(options.get("max_reviews", 5_000)), 50_000))
        max_pages = int(options.get("max_pages", 0))
        max_seconds = int(options.get("max_seconds", 0))
        started = time.monotonic()
        scanned = positive = pages = 0
        languages: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        stop_reason = "end_of_corpus"
        while scanned < max_reviews:
            if job_id:
                await self._checkpoint(job_id)
            if max_pages and pages >= max_pages:
                stop_reason = "max_pages"
                break
            if max_seconds and time.monotonic() - started >= max_seconds:
                stop_reason = "max_seconds"
                break
            page_size = min(100, max_reviews - scanned)
            page = await self.call(
                "steam_get_app_review_batch",
                {
                    "appid": appid,
                    "cursor": cursor,
                    "sort_by": options.get("sort_by", "recent"),
                    "page_size": page_size,
                    "review_type": options.get("review_type", "all"),
                    "purchase_type": options.get("purchase_type", "all"),
                    "language": options.get("language", "all"),
                    "include_offtopic_activity": bool(options.get("include_offtopic_activity", False)),
                    "max_text_chars": min(int(options.get("max_text_chars", 1_200)), 4_000),
                    "include_author_id": bool(options.get("include_author_id", False)),
                    "country_code": options.get("country", "us"),
                },
                ttl=60,
            )
            reviews = page.get("reviews", []) if isinstance(page, dict) else []
            for review in reviews:
                scanned += 1
                positive += int(bool(review.get("voted_up")))
                language = str(review.get("language") or "unknown")
                languages[language] = languages.get(language, 0) + 1
                if len(samples) < int(options.get("sample_per_bucket", 4)) * 2:
                    samples.append(review)
            pages += 1
            page_info = page.get("page", {}) if isinstance(page, dict) else {}
            next_cursor = page_info.get("next_cursor")
            if job_id:
                await self._checkpoint(job_id)
            if not page_info.get("has_more") or not next_cursor:
                break
            cursor = str(next_cursor)
        return {
            "appid": appid,
            "reviews_scanned": scanned,
            "positive": positive,
            "negative": scanned - positive,
            "positive_pct": round(positive * 100 / scanned, 1) if scanned else None,
            "languages": languages,
            "pages_fetched": pages,
            "stop_reason": stop_reason,
            "continuation_cursor": cursor if stop_reason != "end_of_corpus" else None,
            "samples": samples,
        }

    async def _checkpoint(self, job_id: str) -> None:
        job = await self._require_job(job_id)
        if job.status in {"cancel_requested", "cancelled"}:
            raise _Cancelled()

    async def get(self, job_id: str, cursor: str, limit: int, max_chars: int) -> dict[str, Any]:
        job = await self._require_job(job_id)
        envelope = job_envelope(self.public_job(job))
        if job.status != "succeeded" or not job.result_ref:
            return envelope
        result = await self.result_store.get(job.result_ref)
        page_limit = bounded_limit(limit, 20, 100)
        offset = 0
        filters = {"job_id": job_id, "limit": page_limit}
        if cursor:
            offset = int(self.cursor.decode(cursor, scope="job:result", filters=filters).get("offset", 0))
        items, container = self._pageable(result)
        if items is None:
            envelope["data"], truncated = bounded_value(result, max(500, min(max_chars, 32_000)))
            if truncated:
                envelope["meta"]["warnings"].append("Job result text was truncated to the requested budget.")
            return envelope
        page = items[offset:offset + page_limit]
        has_more = offset + len(page) < len(items)
        envelope["data"] = container
        envelope["items"] = page
        envelope["page"] = {
            "returned": len(page),
            "has_more": has_more,
            "next_cursor": self.cursor.encode(
                scope="job:result",
                filters=filters,
                state={"offset": offset + len(page)},
                expires_at=job.expires_at,
            ) if has_more else None,
        }
        return envelope

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return job_envelope(self.public_job(await self.job_store.request_cancel(job_id)))

    async def _require_job(self, job_id: str) -> JobRecord:
        job = await self.job_store.get(job_id)
        if job is None:
            raise ServiceError(ErrorCode.JOB_EXPIRED, "The job does not exist or has expired.")
        return job

    @staticmethod
    def public_job(job: JobRecord) -> dict[str, Any]:
        def stamp(value: float) -> str:
            return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")

        return {
            "job_id": job.job_id,
            "task": job.task,
            "status": job.status,
            "progress": job.progress,
            "error": job.error,
            "status_uri": f"steam://job/{job.job_id}",
            "result_uri": f"steam://job/{job.job_id}/result/_" if job.result_ref else None,
            "created_at": stamp(job.created_at),
            "updated_at": stamp(job.updated_at),
            "expires_at": stamp(job.expires_at),
        }

    @staticmethod
    def _user(ref: str) -> str:
        return ref.removeprefix("steam://entity/user/")

    @staticmethod
    def _need(refs: list[str], count: int, task: str) -> None:
        if len(refs) < count:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"{task} requires {count} references.")

    @staticmethod
    def _pageable(result: Any) -> tuple[list[Any] | None, Any]:
        if isinstance(result, list):
            return result, None
        if isinstance(result, dict):
            for key, value in result.items():
                if isinstance(value, list) and len(value) > 20:
                    return value, {k: v for k, v in result.items() if k != key}
        return None, result
