"""Bounded operation logs and slow-thread snapshots without local variables."""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from functools import wraps

logger = logging.getLogger(__name__)


def safe_error(value: object) -> str:
    text = str(value)
    text = re.sub(r"https?://[^\s\"'<>]+", "[url]", text)
    text = re.sub(r"\b(?:rk-|st-)[A-Za-z0-9_-]+", "[secret]", text)
    text = re.sub(
        r"(?i)\b(?:bearer\s+\S+|(?:[a-z_]*token|api[_-]?key|authorization|password)"
        r"""["']?\s*[:=]\s*["']?[^,\s}"']+)""",
        "[secret]",
        text,
    )
    return " ".join(text.split())[:400]


@contextmanager
def diagnostic_span(event: str, *, slow_seconds: float = 5.0, **fields):
    operation_id = uuid.uuid4().hex[:12]
    thread_id = threading.get_ident()
    started = time.monotonic()
    finished = threading.Event()

    def emit(phase: str, *, level=logging.INFO, **extra):
        logger.log(level, json.dumps({
            "event": event,
            "operation_id": operation_id,
            "thread_id": thread_id,
            "phase": phase,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            **fields,
            **extra,
        }, ensure_ascii=False))

    def slow():
        if finished.is_set():
            return
        frame = sys._current_frames().get(thread_id)
        stack = [
            f"{entry.filename}:{entry.lineno} in {entry.name}"
            for entry in traceback.extract_stack(frame, limit=16)
        ] if frame else []
        emit("slow", level=logging.WARNING, stack=stack)

    emit("start")
    watchdog = threading.Timer(slow_seconds, slow)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    except Exception as exc:
        emit("error", level=logging.WARNING,
             error_type=type(exc).__name__, error=safe_error(exc))
        raise
    finally:
        finished.set()
        watchdog.cancel()
        emit("done")


def traced(event: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with diagnostic_span(event):
                return function(*args, **kwargs)
        return wrapped
    return decorate
