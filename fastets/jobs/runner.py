"""Background job execution with an event stream the UI can poll."""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple


def _as_text(value: Any) -> str:
    """Render any value as a single readable line of text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, Mapping):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (list, tuple)):
        return "、".join(_as_text(item) for item in value)
    return str(value)


def _normalise_progress(
    message: Any, percent: Optional[float], extra: Dict[str, Any]
) -> Tuple[str, Optional[float], Dict[str, Any]]:
    """Split a progress payload into ``(text, percent, extra)``."""
    if not isinstance(message, Mapping):
        return _as_text(message), percent, dict(extra)

    payload: Dict[str, Any] = dict(message)
    text = _as_text(payload.pop("message", ""))
    inner_percent = payload.pop("percent", None)
    if percent is None:
        percent = inner_percent if isinstance(inner_percent, (int, float)) else None
    merged = dict(payload)
    merged.update(extra)
    return text, percent, merged


class Job:
    def __init__(self, kind: str, title: str) -> None:
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.title = title
        self.status = "queued"      # queued | running | succeeded | failed | cancelled
        self.message = "等待执行"
        self.percent: Optional[float] = None
        self.result: Any = None
        self.error: str = ""
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "percent": self.percent,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobRunner:
    """Runs one job at a time on a worker thread and records its events."""

    def __init__(self, logger=None) -> None:
        self._log = logger
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._jobs: Dict[str, Job] = {}
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._current: Optional[Job] = None

    # -- events --------------------------------------------------------------

    def _push(self, event: Dict[str, Any]) -> None:
        with self._lock:
            event["seq"] = len(self._events)
            self._events.append(event)
            if len(self._events) > 2000:
                del self._events[:1000]

    def events_since(self, seq: int = 0) -> Dict[str, Any]:
        with self._lock:
            pending = [e for e in self._events if e.get("seq", 0) >= seq]
            return {"events": pending, "next": len(self._events)}

    def job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    # -- lifecycle -----------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def start(self, kind: str, title: str, func: Callable[..., Any], *args, **kwargs) -> Job:
        if self.busy:
            raise RuntimeError("已有任务正在运行，请先等待或停止")
        job = Job(kind, title)
        with self._lock:
            self._jobs[job.id] = job
            self._current = job
            self._cancel = threading.Event()
        self._push({"type": "job", "job": job.to_dict()})

        def worker() -> None:
            job.status = "running"
            job.message = "执行中"
            self._push({"type": "job", "job": job.to_dict()})
            try:
                result = func(*args, **kwargs)
                job.result = result
                job.status = "succeeded"
                job.message = "完成"
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                job.status = "failed"
                job.error = str(exc)
                job.message = f"失败: {exc}"
                if self._log:
                    self._log.error("job %s failed: %s", job.id, exc)
                    self._log.debug(traceback.format_exc())
            finally:
                job.finished_at = time.time()
                self._push({"type": "job", "job": job.to_dict()})

        thread = threading.Thread(target=worker, name=f"fastets-{kind}", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return job

    def stop(self) -> bool:
        with self._lock:
            job = self._current
            self._cancel.set()
        if job and job.status == "running":
            job.status = "cancelled"
            job.message = "已取消"
            self._push({"type": "job", "job": job.to_dict()})
            return True
        return False

    def progress(
        self,
        message: Any,
        percent: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Emit a progress event for the running job.

        ``message`` may be a plain string or a mapping. The completion pipeline
        reports structured payloads (``{"stage": ..., "message": ..., ...}``);
        normalising them here keeps the UI from ever rendering a raw object.
        """
        text, percent, extra = _normalise_progress(message, percent, extra)
        with self._lock:
            job = self._current
        if job:
            job.message = text
            if percent is not None:
                job.percent = percent
        event = {"type": "progress", "message": text, "percent": percent}
        event.update(extra)
        self._push(event)

    def log(self, message: Any, level: str = "info", **extra: Any) -> None:
        event = {"type": "log", "message": _as_text(message), "level": level}
        event.update(extra)
        self._push(event)
