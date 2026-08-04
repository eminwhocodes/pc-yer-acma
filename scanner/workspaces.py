"""Scan local app / repo workspaces (c:\\www, C:\\wamp64\\www)."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .disk import dir_size

WORKSPACE_ROOTS = [
    r"c:\www",
    r"C:\wamp64\www",
]

# Inside a project, these folders often dominate size
HEAVY_NAMES = {
    "node_modules",
    "vendor",
    ".git",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".output",
    "target",
    "storage",
    "cache",
    ".cache",
    "tmp",
    "temp",
    "uploads",
    "public",
    "coverage",
    "venv",
    ".venv",
    "__pycache__",
    "bower_components",
    "Pods",
}

EXPAND_IF_BYTES = 150 * 1024 * 1024  # 150 MB
TOP_CHILDREN = 12

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "current": "",
    "roots": [],
    "started": None,
    "finished": None,
    "error": None,
    "from_cache": False,
    "cached_at": None,
}


def get_workspace_state() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _set(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def hydrate_workspaces(payload: dict[str, Any] | None, saved_at: float | None = None) -> None:
    if not payload:
        return
    roots = payload.get("roots") or []
    if not roots:
        return
    _set(
        status="done" if payload.get("status") == "done" else payload.get("status") or "done",
        progress=payload.get("progress", 100),
        current="",
        roots=roots,
        started=None,
        finished=payload.get("finished") or saved_at,
        error=None,
        from_cache=True,
        cached_at=saved_at,
    )


def export_workspaces_for_cache() -> dict[str, Any]:
    with _lock:
        return {
            "status": _state.get("status"),
            "progress": _state.get("progress", 0),
            "roots": _state.get("roots") or [],
            "finished": _state.get("finished"),
        }


def _measure_entry(path: Path) -> dict[str, Any]:
    b, e = dir_size(path, timeout=90)
    node: dict[str, Any] = {
        "path": str(path),
        "label": path.name,
        "bytes": b,
        "errors": e,
        "is_dir": path.is_dir(),
        "children": [],
    }
    if path.is_dir() and b >= EXPAND_IF_BYTES:
        node["children"] = _heavy_children(path)
    return node


def _heavy_children(path: Path) -> list[dict]:
    kids: list[dict] = []
    try:
        entries = list(path.iterdir())
    except OSError:
        return kids

    # Prefer known heavy names; also measure other large dirs (cap)
    named = []
    others = []
    for p in entries:
        try:
            if p.is_symlink():
                continue
            if not p.is_dir() and not p.is_file():
                continue
            if p.name.lower() in HEAVY_NAMES or p.name in HEAVY_NAMES:
                named.append(p)
            elif p.is_dir():
                others.append(p)
        except OSError:
            continue

    targets = named + others[:20]

    def one(p: Path) -> dict | None:
        try:
            b, e = dir_size(p, timeout=45)
        except Exception:
            return None
        if b < 20 * 1024 * 1024 and p.name.lower() not in HEAVY_NAMES:
            return None
        return {
            "path": str(p),
            "label": p.name,
            "bytes": b,
            "errors": e,
            "is_dir": p.is_dir(),
            "children": [],
        }

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(one, p) for p in targets]
        for fut in as_completed(futs):
            try:
                row = fut.result()
            except Exception:
                continue
            if row:
                kids.append(row)

    kids.sort(key=lambda x: x["bytes"], reverse=True)
    return kids[:TOP_CHILDREN]


def _scan_root(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {
            "path": str(root),
            "label": str(root),
            "bytes": 0,
            "missing": True,
            "projects": [],
        }

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        return {
            "path": str(root),
            "label": str(root),
            "bytes": 0,
            "missing": False,
            "error": str(e),
            "projects": [],
        }

    projects: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_measure_entry, p): p for p in entries}
        done = 0
        total = max(len(futs), 1)
        for fut in as_completed(futs):
            p = futs[fut]
            done += 1
            _set(current=str(p), progress=min(99, int(done / total * 100)))
            try:
                projects.append(fut.result())
            except Exception as e:
                projects.append(
                    {
                        "path": str(p),
                        "label": p.name,
                        "bytes": 0,
                        "errors": 1,
                        "is_dir": False,
                        "children": [],
                        "error": str(e),
                    }
                )

    projects.sort(key=lambda x: x["bytes"], reverse=True)
    return {
        "path": str(root),
        "label": str(root),
        "bytes": sum(p["bytes"] for p in projects),
        "missing": False,
        "projects": projects,
    }


def _run_workspaces() -> None:
    _set(
        status="running",
        progress=0,
        current="",
        started=time.time(),
        finished=None,
        error=None,
        from_cache=False,
        cached_at=None,
    )
    roots_out: list[dict] = []
    try:
        root_paths = [Path(p) for p in WORKSPACE_ROOTS]
        for i, root in enumerate(root_paths):
            _set(current=str(root), progress=int(i / max(len(root_paths), 1) * 100))
            roots_out.append(_scan_root(root))
            _set(roots=list(roots_out))
        finished = time.time()
        _set(
            status="done",
            progress=100,
            current="",
            finished=finished,
            roots=roots_out,
            from_cache=False,
            cached_at=finished,
        )
        # persist via categories helper if available
        try:
            from . import categories as cats

            cats._persist_scans()
        except Exception:
            pass
    except Exception as e:
        _set(status="error", error=str(e), finished=time.time())


def start_workspace_scan(background: bool = True) -> dict[str, Any]:
    with _lock:
        if _state["status"] == "running":
            return get_workspace_state()
    if background:
        threading.Thread(target=_run_workspaces, daemon=True, name="workspace-scan").start()
        return get_workspace_state()
    _run_workspaces()
    return get_workspace_state()
