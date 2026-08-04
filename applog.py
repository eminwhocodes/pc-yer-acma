"""File logging with 7-day retention."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
RETENTION_DAYS = 7
_lock = threading.Lock()
_last_purge = 0.0


def _purge_old() -> None:
    global _last_purge
    now = time.time()
    if now - _last_purge < 3600:
        return
    _last_purge = now
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
        for p in LOG_DIR.glob("*.log"):
            try:
                # filename YYYY-MM-DD.log or mtime
                stem = p.stem
                try:
                    file_day = datetime.strptime(stem[:10], "%Y-%m-%d")
                except ValueError:
                    file_day = datetime.fromtimestamp(p.stat().st_mtime)
                if file_day < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                    p.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def log(message: str, level: str = "INFO", **fields: Any) -> None:
    _purge_old()
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": level,
        "msg": message,
    }
    for k, v in fields.items():
        try:
            json.dumps(v, default=str)
            record[k] = v
        except Exception:
            record[k] = str(v)
    line = json.dumps(record, ensure_ascii=False, default=str)
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
    with _lock:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def log_exception(message: str, exc: BaseException, **fields: Any) -> None:
    log(message, level="ERROR", error=str(exc), error_type=type(exc).__name__, **fields)
