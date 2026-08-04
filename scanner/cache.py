"""Persist last scan results for instant UI on restart."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data"
CACHE_FILE = CACHE_DIR / "last_scan.json"

# Auto-rescan if cache older than this (seconds)
STALE_AFTER = 60 * 60  # 1 hour


def load_cache() -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def save_cache(payload: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(payload)
        payload["saved_at"] = time.time()
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


def cache_age_seconds(cache: dict[str, Any] | None = None) -> float | None:
    data = cache if cache is not None else load_cache()
    if not data:
        return None
    saved = data.get("saved_at")
    if not saved:
        return None
    return max(0.0, time.time() - float(saved))


def is_stale(cache: dict[str, Any] | None = None, max_age: int = STALE_AFTER) -> bool:
    age = cache_age_seconds(cache)
    if age is None:
        return True
    return age > max_age
