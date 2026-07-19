from __future__ import annotations

import hmac
import os
import threading
import time
from typing import Any

from core import db
from core import registration_service as registration


def _float(value: object, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _token() -> str:
    configured = str(os.getenv("REGISTER_CONTROL_TOKEN") or "").strip()
    if configured:
        return configured
    try:
        from config import register
        return str(getattr(register, "REGISTER_CONTROL_TOKEN", "") or "").strip()
    except Exception:
        return ""


def configured() -> bool:
    return bool(_token())


def authorize(value: str | None) -> bool:
    supplied = str(value or "").strip()
    configured_token = _token()
    return bool(supplied and configured_token and hmac.compare_digest(supplied, configured_token))


class ExternalRegisterController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled = False
        self._target_inflight = 0
        self._workers = 1
        self._min_interval_seconds = 0.0
        self._last_submit_at = 0.0
        self._last_reconcile_at = 0.0
        self._last_error = ""
        self._last_reason = ""

    @staticmethod
    def _job_stats() -> dict[str, int]:
        rows = db.list_jobs(limit=100000)
        counts = {"pending": 0, "running": 0, "success": 0, "failed": 0, "stopped": 0, "cancelled": 0}
        for row in rows:
            status = str(row.get("status") or "")
            if status in counts:
                counts[status] += 1
        counts["active"] = counts["pending"] + counts["running"]
        counts["total"] = len(rows)
        return counts

    def status(self) -> dict[str, Any]:
        jobs = self._job_stats()
        with self._lock:
            return {
                "enabled": self._enabled,
                "target_inflight": self._target_inflight,
                "workers": self._workers,
                "min_interval_seconds": self._min_interval_seconds,
                **jobs,
                "executor_workers": registration.get_executor_workers(),
                "last_submit_at": self._last_submit_at or None,
                "last_reconcile_at": self._last_reconcile_at or None,
                "last_reason": self._last_reason,
                "last_error": self._last_error,
            }

    def reconcile(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = max(0, min(32, _int(payload.get("target_inflight"), 0)))
        workers = max(1, min(32, _int(payload.get("workers"), max(1, target))))
        interval = min(3600.0, _float(payload.get("min_interval_seconds"), 0.0))
        enabled = bool(payload.get("enabled", True))
        reason = str(payload.get("reason") or "").strip()[:240]
        now = time.time()
        with self._lock:
            self._enabled = enabled
            self._target_inflight = target
            self._workers = workers
            self._min_interval_seconds = interval
            self._last_reconcile_at = now
            self._last_reason = reason
            self._last_error = ""

        jobs = self._job_stats()
        active = jobs["active"]
        submitted = 0
        if enabled and target > active:
            with self._lock:
                rate_ready = now - self._last_submit_at >= self._min_interval_seconds
            if rate_ready:
                count = min(target - active, workers)
                try:
                    jobs = registration.submit_registration(
                        count=count,
                        email_source="external",
                        workers=workers,
                    )
                    submitted = len(jobs)
                    with self._lock:
                        self._last_submit_at = time.time()
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"{type(exc).__name__}: {exc}"[:500]

        result = self.status()
        result["submitted"] = submitted
        return result


external_register_controller = ExternalRegisterController()
