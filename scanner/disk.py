"""Disk and folder size helpers."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Iterable


def get_disk_summary(path: str = "C:\\") -> dict:
    usage = shutil.disk_usage(path)
    total = usage.total
    used = usage.used
    free = usage.free
    return {
        "path": path,
        "total": total,
        "used": used,
        "free": free,
        "percent": round((used / total) * 100, 1) if total else 0,
    }


def _dir_size_robocopy(path: str | Path, timeout: int = 120) -> tuple[int, int] | None:
    """Fast folder size via robocopy list-only mode (Windows)."""
    src = str(Path(path))
    if not os.path.exists(src):
        return 0, 0
    if os.path.isfile(src):
        try:
            return os.path.getsize(src), 0
        except OSError:
            return 0, 1
    # Dummy dest — /L = list only, no copy
    dest = src
    try:
        r = subprocess.run(
            [
                "robocopy",
                src,
                dest,
                "/L",
                "/S",
                "/NJH",
                "/NJS",
                "/NC",
                "/NDL",
                "/NP",
                "/BYTES",
                "/NFL",
                "/XJ",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        # robocopy exit codes 0-7 are success-ish
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        # Look for Bytes : NNNNN
        m = re.search(r"Bytes\s*:\s*([\d,]+)", out, re.IGNORECASE)
        if not m:
            # Newer robocopy: "Bytes :  123456" in summary lines
            m = re.search(r"Bytes\s+:\s+([\d\s]+)", out)
        if m:
            num = m.group(1).replace(",", "").replace(" ", "")
            if num.isdigit():
                return int(num), 0
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    return None


def _dir_size_walk(path: str | Path) -> tuple[int, int]:
    root = Path(path)
    if not root.exists():
        return 0, 0
    if root.is_file():
        try:
            return root.stat().st_size, 0
        except OSError:
            return 0, 1

    total = 0
    errors = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            cleaned = []
            for d in dirnames:
                full = Path(dirpath) / d
                try:
                    if full.is_symlink():
                        continue
                    attrs = getattr(full.lstat(), "st_file_attributes", 0)
                    if attrs & 0x400:
                        continue
                except OSError:
                    errors += 1
                    continue
                cleaned.append(d)
            dirnames[:] = cleaned
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    errors += 1
                    if errors > 2000:
                        return total, errors
    except OSError:
        errors += 1
    return total, errors


def dir_size(path: str | Path, timeout: int = 90) -> tuple[int, int]:
    """Prefer robocopy (fast on Windows), fallback to walk with timeout."""
    result: list[tuple[int, int]] = []

    def worker() -> None:
        rc = _dir_size_robocopy(path, timeout=timeout)
        if rc is not None:
            result.append(rc)
        else:
            result.append(_dir_size_walk(path))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout + 5)
    if t.is_alive():
        return 0, 1
    return result[0] if result else (0, 1)


def first_existing(candidates: Iterable[str | Path]) -> str | None:
    for c in candidates:
        p = Path(os.path.expandvars(str(c)))
        if p.exists():
            return str(p)
    return None


def measure_paths(paths: list[str], label_map: dict[str, str] | None = None) -> list[dict]:
    items = []
    for p in paths:
        expanded = os.path.expandvars(p)
        if not Path(expanded).exists():
            continue
        size, err = dir_size(expanded)
        items.append(
            {
                "path": expanded,
                "label": (label_map or {}).get(p, Path(expanded).name),
                "bytes": size,
                "errors": err,
                "exists": True,
            }
        )
    items.sort(key=lambda x: x["bytes"], reverse=True)
    return items
