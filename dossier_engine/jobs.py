"""In-process async job store for Stage C analyze (Task T18 / Finding #14).

Pattern: submit → job_id → poll progress/result. C's UI polls its own jobs —
not a mega job dashboard.
"""
from __future__ import annotations

import hashlib
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

ProgressCb = Callable[[str, int, str, Optional[Dict[str, Any]]], None]


def _env_float(name: str, default: float) -> float:
    import os

    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    import os

    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class JobRecord:
    job_id: str
    kind: str
    status: str  # queued | running | succeeded | failed | timed_out
    created_at: float
    updated_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress_stage: str = "queued"
    progress_percent: int = 0
    progress_message: str = "Queued"
    progress_detail: Dict[str, Any] = field(default_factory=dict)
    request: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    idempotency_key: Optional[str] = None
    timeout_s: float = 0.0


class JobStore:
    def __init__(
        self,
        *,
        max_workers: int | None = None,
        default_timeout_s: float | None = None,
        retention_s: float | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, JobRecord] = {}
        self._by_idem: Dict[str, str] = {}
        self.max_workers = int(
            max_workers if max_workers is not None else _env_int("ANALYZE_MAX_CONCURRENT_JOBS", 2)
        )
        self.default_timeout_s = float(
            default_timeout_s
            if default_timeout_s is not None
            else _env_float("ANALYZE_JOB_TIMEOUT_S", 540)
        )
        self.retention_s = float(
            retention_s
            if retention_s is not None
            else _env_float("ANALYZE_JOB_RETENTION_S", 3600)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.max_workers),
            thread_name_prefix="c-analyze-job",
        )

    def submit_analyze(
        self,
        *,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        timeout_s: Optional[float] = None,
        runner: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> JobRecord:
        run_fn = runner
        if run_fn is None:
            # Lazy import avoids circular import at module load (webserver → jobs).
            from .webserver import _analyze as default_analyze

            run_fn = default_analyze
        key = (idempotency_key or "").strip() or None
        if key:
            body_fp = hashlib.sha256(
                repr(sorted(payload.items())).encode("utf-8")
            ).hexdigest()[:16]
            # Prefer stable JSON when possible.
            try:
                import json

                body_fp = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()[:16]
            except Exception:  # noqa: BLE001
                pass
            composite = f"{key}:{body_fp}"
        else:
            composite = None

        with self._lock:
            self._prune_locked()
            if composite and composite in self._by_idem:
                existing = self._jobs.get(self._by_idem[composite])
                if existing and existing.status in (
                    "queued",
                    "running",
                    "succeeded",
                ):
                    return existing
                self._by_idem.pop(composite, None)

            job_id = str(uuid.uuid4())
            now = time.time()
            # Drop large blobs from stored request (keep product name if present).
            safe_req = {
                "product_name": payload.get("product_name"),
                "n_ingredients": len(payload.get("ingredients") or []),
                "use_auth": payload.get("use_auth", True),
            }
            rec = JobRecord(
                job_id=job_id,
                kind="analyze",
                status="queued",
                created_at=now,
                updated_at=now,
                request=safe_req,
                idempotency_key=key,
                timeout_s=float(
                    timeout_s if timeout_s is not None else self.default_timeout_s
                ),
            )
            self._jobs[job_id] = rec
            if composite:
                self._by_idem[composite] = job_id

        self._executor.submit(self._run_analyze, job_id, run_fn, payload)
        return rec

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            self._prune_locked()
            return self._jobs.get(job_id)

    def public_view(self, rec: JobRecord) -> Dict[str, Any]:
        view: Dict[str, Any] = {
            "job_id": rec.job_id,
            "kind": rec.kind,
            "status": rec.status,
            "created_at": _iso(rec.created_at),
            "updated_at": _iso(rec.updated_at),
            "started_at": _iso(rec.started_at) if rec.started_at else None,
            "finished_at": _iso(rec.finished_at) if rec.finished_at else None,
            "timeout_s": rec.timeout_s,
            "progress": {
                "stage": rec.progress_stage,
                "percent": rec.progress_percent,
                "message": rec.progress_message,
                "detail": dict(rec.progress_detail),
            },
            "poll_url": f"/api/jobs/{rec.job_id}",
            "result_url": f"/api/jobs/{rec.job_id}/result",
        }
        if rec.status == "succeeded" and rec.result is not None:
            view["result"] = rec.result
        if rec.status in ("failed", "timed_out") and rec.error:
            view["error"] = rec.error
        return view

    def _set_progress(
        self,
        job_id: str,
        stage: str,
        percent: int,
        message: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec or rec.status not in ("queued", "running"):
                return
            rec.progress_stage = stage
            rec.progress_percent = max(0, min(100, int(percent)))
            rec.progress_message = message
            if detail is not None:
                rec.progress_detail = dict(detail)
            rec.updated_at = time.time()

    def _run_analyze(
        self,
        job_id: str,
        run_fn: Callable[..., Dict[str, Any]],
        payload: Dict[str, Any],
    ) -> None:
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                return
            rec.status = "running"
            rec.started_at = time.time()
            rec.updated_at = rec.started_at
            timeout_s = rec.timeout_s

        def on_progress(
            stage: str,
            percent: int,
            message: str,
            detail: Optional[Dict[str, Any]] = None,
        ) -> None:
            self._set_progress(job_id, stage, percent, message, detail)

        deadline = time.time() + timeout_s
        result_box: Dict[str, Any] = {}
        error_box: Dict[str, Any] = {}

        def _target() -> None:
            try:
                try:
                    result_box["value"] = run_fn(payload, on_progress=on_progress)
                except TypeError:
                    result_box["value"] = run_fn(payload)
            except Exception as exc:  # noqa: BLE001
                error_box["exc"] = exc
                error_box["tb"] = traceback.format_exc()

        worker = threading.Thread(
            target=_target, name=f"c-analyze-{job_id[:8]}", daemon=True
        )
        worker.start()
        remaining = max(0.1, deadline - time.time())
        worker.join(timeout=remaining)
        if worker.is_alive():
            self._finish(
                job_id,
                status="timed_out",
                error=f"Analyze job exceeded wall-clock budget ({timeout_s:.0f}s)",
                progress=("timed_out", 100, "Timed out"),
            )
            return
        if "exc" in error_box:
            self._finish(
                job_id,
                status="failed",
                error=str(error_box["exc"]),
                progress=("failed", 100, "Failed"),
            )
            return
        self._finish(
            job_id,
            status="succeeded",
            result=result_box.get("value"),
            progress=("done", 100, "Complete"),
        )

    def _finish(
        self,
        job_id: str,
        *,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        progress: tuple[str, int, str] = ("done", 100, "Complete"),
    ) -> None:
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                return
            now = time.time()
            rec.status = status
            rec.result = result
            rec.error = error
            rec.finished_at = now
            rec.updated_at = now
            rec.progress_stage, rec.progress_percent, rec.progress_message = progress

    def _prune_locked(self) -> None:
        cutoff = time.time() - self.retention_s
        dead = [
            jid
            for jid, rec in self._jobs.items()
            if rec.finished_at and rec.finished_at < cutoff
        ]
        for jid in dead:
            self._jobs.pop(jid, None)
            drop = [k for k, v in self._by_idem.items() if v == jid]
            for k in drop:
                self._by_idem.pop(k, None)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


STORE = JobStore()
