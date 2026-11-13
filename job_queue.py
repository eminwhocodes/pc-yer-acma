"""Sequential job queue for cleanup actions."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from cleaner import ACTIONS, run_action
from applog import log

_lock = threading.Lock()
_jobs: list[dict[str, Any]] = []
_worker_started = False
_cv = threading.Condition(_lock)


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="clean-queue").start()


def _summarize_result(result: dict[str, Any]) -> str | None:
    if result.get("freed_bytes"):
        b = int(result["freed_bytes"])
        if b >= 1024**3:
            return f"~{b / 1024**3:.1f} GB boşaldı"
        if b >= 1024**2:
            return f"~{b / 1024**2:.0f} MB boşaldı"
        return f"{b} B boşaldı"
    if result.get("note"):
        return str(result["note"])[:180]
    if result.get("error"):
        return str(result["error"])[:180]
    return None


def _worker_loop() -> None:
    while True:
        job = None
        with _cv:
            while True:
                pending = [j for j in _jobs if j["status"] == "queued"]
                if pending:
                    job = pending[0]
                    job["status"] = "running"
                    job["started_at"] = time.time()
                    break
                _cv.wait(timeout=1.0)
                continue
        if job is None:
            continue
        log("job_running", job_id=job["id"], action=job["action"])
        try:
            fn = job.get("fn")
            result = fn(job) if fn else run_action(job["action"])
            with _lock:
                if job.get("cancel_requested") and result.get("cancelled"):
                    job["status"] = "cancelled"
                else:
                    job["status"] = "done" if result.get("ok") else "error"
                job["finished_at"] = time.time()
                job["result"] = result
                job["summary"] = _summarize_result(result)
                job["freed_bytes"] = result.get("freed_bytes")
                if not result.get("ok") and result.get("error"):
                    job["error"] = result["error"]
                elif not result.get("ok"):
                    errs = []
                    for d in result.get("details") or []:
                        if isinstance(d, dict) and d.get("errors"):
                            errs.extend(d["errors"][:3])
                        if isinstance(d, dict) and d.get("stderr"):
                            errs.append(d["stderr"])
                    job["error"] = "; ".join(errs[:5]) if errs else (job.get("summary") or "Başarısız")
            log(
                "job_finished",
                job_id=job["id"],
                action=job["action"],
                status=job["status"],
                summary=job.get("summary"),
                freed_bytes=job.get("freed_bytes"),
                error=job.get("error"),
            )
        except Exception as e:
            with _lock:
                job["status"] = "error"
                job["finished_at"] = time.time()
                job["error"] = str(e)
                job["summary"] = str(e)
                job["result"] = {"ok": False, "error": str(e)}
            log("job_exception", level="ERROR", job_id=job["id"], error=str(e))
        with _cv:
            _cv.notify_all()


def enqueue(action_id: str) -> dict[str, Any]:
    if action_id not in ACTIONS:
        return {"ok": False, "error": "Bilinmeyen aksiyon"}
    meta = ACTIONS[action_id]
    job = {
        "id": str(uuid.uuid4())[:8],
        "action": action_id,
        "title": meta["title"],
        "risk": meta["risk"],
        "status": "queued",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
        "summary": None,
        "freed_bytes": None,
    }
    with _cv:
        for j in _jobs:
            if j["action"] == action_id and j["status"] in ("queued", "running"):
                log("job_duplicate", action=action_id, job_id=j["id"])
                return {
                    "ok": True,
                    "queued": True,
                    "duplicate": True,
                    "job": j,
                    "queue": get_queue(),
                }
        _jobs.append(job)
        _ensure_worker()
        _cv.notify_all()
    log("job_queued", job_id=job["id"], action=action_id)
    return {"ok": True, "queued": True, "duplicate": False, "job": job, "queue": get_queue()}


def enqueue_task(action: str, title: str, fn: Callable[[dict[str, Any]], dict[str, Any]], risk: str = "low") -> dict[str, Any]:
    """Fonksiyonla çalışan iş. fn(job) ilerlemeyi update_job ile bildirir,
    job["cancel_requested"] doğruysa erken döner ({"cancelled": True})."""
    job = {
        "id": str(uuid.uuid4())[:8],
        "action": action,
        "title": title,
        "risk": risk,
        "status": "queued",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
        "summary": None,
        "freed_bytes": None,
        "progress": 0,
        "detail": None,
        "cancellable": True,
        "fn": fn,
    }
    with _cv:
        _jobs.append(job)
        _ensure_worker()
        _cv.notify_all()
    log("job_queued", job_id=job["id"], action=action)
    return {"ok": True, "job": _slim(job)}


def update_job(job: dict[str, Any], **kw: Any) -> None:
    with _lock:
        job.update(kw)


def cancel_job(job_id: str) -> dict[str, Any]:
    with _lock:
        for j in _jobs:
            if j["id"] == job_id and j["status"] == "queued":
                j["status"] = "cancelled"
                j["finished_at"] = time.time()
                log("job_cancelled", job_id=job_id)
                return {"ok": True, "job": _slim(j)}
            if j["id"] == job_id and j["status"] == "running" and j.get("cancellable"):
                j["cancel_requested"] = True
                log("job_cancel_requested", job_id=job_id)
                return {"ok": True, "job": _slim(j)}
    return {"ok": False, "error": "İptal edilemedi (sadece sıradakiler)"}


def clear_finished() -> dict[str, Any]:
    with _lock:
        kept = [j for j in _jobs if j["status"] in ("queued", "running")]
        _jobs[:] = kept
    return {"ok": True, "queue": get_queue()}


def _slim(j: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in j.items() if k not in ("result", "fn")}


def get_queue() -> dict[str, Any]:
    with _lock:
        jobs = [dict(j) for j in _jobs]
    slim = []
    for j in jobs:
        item = _slim(j)
        if j.get("result") is not None:
            item["has_result"] = True
            item["result_ok"] = bool(j["result"].get("ok"))
        slim.append(item)
    queued = sum(1 for j in slim if j["status"] == "queued")
    running = sum(1 for j in slim if j["status"] == "running")
    return {
        "jobs": slim[-40:],
        "queued": queued,
        "running": running,
        "active": queued + running,
    }


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        for j in _jobs:
            if j["id"] == job_id:
                return {k: v for k, v in j.items() if k != "fn"}
    return None
