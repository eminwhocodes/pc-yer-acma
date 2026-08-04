"""Category and deep disk scanning with shared state."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .disk import dir_size, first_existing, get_disk_summary, measure_paths
from .docker_scan import docker_daemon_status, docker_system_df
from . import cache as scan_cache

_lock = threading.Lock()
_state: dict[str, Any] = {
    "quick": {
        "status": "idle",
        "categories": [],
        "started": None,
        "finished": None,
        "error": None,
        "from_cache": False,
        "cached_at": None,
    },
    "deep": {
        "status": "idle",
        "progress": 0,
        "current": "",
        "roots": [],
        "started": None,
        "finished": None,
        "error": None,
        "from_cache": False,
        "cached_at": None,
    },
}


def get_scan_state() -> dict[str, Any]:
    with _lock:
        quick = dict(_state["quick"])
        deep = dict(_state["deep"])
    age = None
    if quick.get("cached_at"):
        age = max(0.0, time.time() - float(quick["cached_at"]))
    elif quick.get("finished") and quick.get("from_cache") is False:
        age = max(0.0, time.time() - float(quick["finished"]))
    return {
        "disk": get_disk_summary(),
        "quick": quick,
        "deep": deep,
        "cache": {
            "age_seconds": age,
            "stale_after": scan_cache.STALE_AFTER,
            "path": str(scan_cache.CACHE_FILE),
        },
        "docker_daemon": docker_daemon_status(),
    }


def _set_quick(**kwargs: Any) -> None:
    with _lock:
        _state["quick"].update(kwargs)


def _set_deep(**kwargs: Any) -> None:
    with _lock:
        _state["deep"].update(kwargs)


def _user() -> str:
    return os.environ.get("USERPROFILE", r"C:\Users\Public")


def _local() -> str:
    return os.environ.get("LOCALAPPDATA", os.path.join(_user(), "AppData", "Local"))


def _roaming() -> str:
    return os.environ.get("APPDATA", os.path.join(_user(), "AppData", "Roaming"))


def _cat(
    id_: str,
    name: str,
    paths: list[str],
    cleanable: bool = False,
    clean_actions: list[str] | None = None,
    note: str = "",
    extra: dict | None = None,
) -> dict:
    items = measure_paths(paths)
    total = sum(i["bytes"] for i in items)
    return {
        "id": id_,
        "name": name,
        "bytes": total,
        "items": items,
        "cleanable": cleanable,
        "clean_actions": clean_actions or [],
        "note": note,
        "extra": extra or {},
    }


def _scan_docker() -> dict:
    df = docker_system_df()
    items = []
    if df.get("available"):
        for t in df.get("types", []):
            items.append(
                {
                    "path": f"docker:{t['type']}",
                    "label": f"{t['type']} ({t.get('total_count', '?')})",
                    "bytes": t.get("size_bytes", 0),
                    "errors": 0,
                    "exists": True,
                    "reclaimable": t.get("reclaimable", ""),
                }
            )
    daemon = df.get("daemon") or {}
    if df.get("available"):
        note = "Temizlik docker CLI ile yapılır (dosya silinmez)."
    elif daemon.get("cli") and not daemon.get("running"):
        note = f"UYARI: {daemon.get('message') or 'Docker kapalı'} — {daemon.get('hint') or ''}"
    else:
        note = df.get("error") or "Docker bulunamadı."
    return {
        "id": "docker",
        "name": "Docker",
        "bytes": df.get("total_bytes", 0),
        "items": items,
        "cleanable": bool(df.get("available")),
        "clean_actions": [
            "docker_containers",
            "docker_dangling_images",
            "docker_unused_images",
            "docker_volumes",
            "docker_build_cache",
            "docker_system_prune",
        ],
        "note": note,
        "extra": df,
    }


def _scan_cursor() -> dict:
    u, la = _user(), _local()
    paths = [
        os.path.join(u, ".cursor"),
        os.path.join(la, "Cursor"),
        os.path.join(la, "cursor-agent"),
        os.path.join(u, ".cursor-sandbox"),
    ]
    return _cat(
        "cursor",
        "Cursor / IDE cache",
        paths,
        cleanable=True,
        clean_actions=["cursor_cache"],
        note="Cache klasörleri temizlenebilir; ayarlar korunmaya çalışılır.",
    )


def _scan_bluestacks() -> dict:
    paths = [
        r"C:\ProgramData\BlueStacks_nxt",
        r"C:\ProgramData\BlueStacks",
        r"C:\Program Files\BlueStacks_nxt",
        r"C:\Program Files\BlueStacks",
        os.path.join(_local(), "BlueStacks"),
        os.path.join(_local(), "Bluestacks"),
    ]
    return _cat(
        "bluestacks",
        "BlueStacks",
        paths,
        cleanable=False,
        note="Rapor only — silme yok (veri kaybı riski).",
    )


def _scan_ldplayer() -> dict:
    paths = [
        r"C:\LDPlayer",
        r"C:\Program Files\LDPlayer",
        r"C:\Program Files (x86)\LDPlayer",
        os.path.join(_local(), "ldplayer"),
        os.path.join(_local(), "Nox"),
        r"C:\Program Files\Nox",
        r"C:\Program Files (x86)\Nox",
    ]
    return _cat(
        "ldplayer",
        "LDPlayer / Emülatör",
        paths,
        cleanable=False,
        note="Rapor only — silme yok.",
    )


def _scan_drive() -> dict:
    """Google Drive / OneDrive / Dropbox — especially offline & cache footprints."""
    u, la = _user(), _local()
    items: list[dict] = []

    def add(path: str, label: str) -> None:
        if not path or not Path(path).exists():
            return
        b, e = dir_size(path, timeout=60)
        items.append(
            {
                "path": path,
                "label": label,
                "bytes": b,
                "errors": e,
                "exists": True,
            }
        )

    drivefs = os.path.join(la, "Google", "DriveFS")
    add(drivefs, "Google DriveFS (toplam)")
    add(os.path.join(drivefs, "cef_cache"), "DriveFS · UI cache (cef)")
    add(os.path.join(drivefs, "Logs"), "DriveFS · loglar")

    # Account folders: content_cache ≈ çevrimdışı / streaming cache
    if Path(drivefs).exists():
        try:
            for acc in Path(drivefs).iterdir():
                if not acc.is_dir():
                    continue
                # numeric account id folders
                if acc.name.isdigit():
                    add(str(acc / "content_cache"), f"DriveFS · çevrimdışı/cache ({acc.name[-6:]})")
                    add(str(acc / "thumbnails_cache"), f"DriveFS · thumbnail ({acc.name[-6:]})")
                    # leftover mirror metadata / dbs sometimes large
                    for name in ("mirror_sqlite.db", "metadata_sqlite.db", "changes_campus_sqlite.db"):
                        p = acc / name
                        if p.exists():
                            try:
                                sz = p.stat().st_size
                            except OSError:
                                continue
                            if sz > 20 * 1024 * 1024:
                                items.append(
                                    {
                                        "path": str(p),
                                        "label": f"DriveFS · {name} ({acc.name[-6:]})",
                                        "bytes": sz,
                                        "errors": 0,
                                        "exists": True,
                                    }
                                )
        except OSError:
            pass

    # Mirrored / classic local Drive folders (full copies on disk)
    for p, label in (
        (os.path.join(u, "Google Drive"), "Google Drive klasörü (mirror/eski)"),
        (os.path.join(u, "My Drive"), "My Drive klasörü (mirror)"),
        (os.path.join(la, "Google", "Drive"), "Google\\Drive (eski client)"),
    ):
        add(p, label)

    # OneDrive: folder size ≈ locally available / "always keep on this device"
    add(os.path.join(u, "OneDrive"), "OneDrive klasörü (yerel kopyalar)")
    add(os.path.join(la, "Microsoft", "OneDrive"), "OneDrive app data")
    # OneDrive standalone update / logs
    add(os.path.join(la, "Microsoft", "OneDrive", "logs"), "OneDrive loglar")
    add(os.path.join(la, "Microsoft", "OneDrive", "setup"), "OneDrive setup cache")

    # Dropbox
    add(os.path.join(u, "Dropbox"), "Dropbox klasörü")
    add(os.path.join(la, "Dropbox"), "Dropbox app data")

    # Deduplicate by path (keep first / more specific already listed)
    seen: set[str] = set()
    uniq: list[dict] = []
    for it in items:
        key = os.path.normcase(it["path"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    uniq.sort(key=lambda x: x["bytes"], reverse=True)

    # Don't double-count DriveFS total + children in category total:
    # use max of DriveFS root OR sum of non-overlapping — simpler: sum all but subtract
    # DriveFS root if children also present.
    drivefs_root = os.path.normcase(drivefs) if Path(drivefs).exists() else None
    child_of_drivefs = [
        i for i in uniq if drivefs_root and os.path.normcase(i["path"]).startswith(drivefs_root + os.sep)
    ]
    has_drivefs_root = any(os.path.normcase(i["path"]) == drivefs_root for i in uniq) if drivefs_root else False
    if has_drivefs_root and child_of_drivefs:
        total = sum(
            i["bytes"]
            for i in uniq
            if os.path.normcase(i["path"]) != drivefs_root
        )
        # DriveFS root already includes children — prefer root size as DriveFS contribution
        root_item = next(i for i in uniq if os.path.normcase(i["path"]) == drivefs_root)
        others = [
            i
            for i in uniq
            if not os.path.normcase(i["path"]).startswith(drivefs_root)
        ]
        total = root_item["bytes"] + sum(i["bytes"] for i in others)
    else:
        total = sum(i["bytes"] for i in uniq)

    offlineish = sum(
        i["bytes"]
        for i in uniq
        if "content_cache" in i["path"].lower()
        or "çevrimdışı" in i["label"].lower()
        or i["label"].startswith("OneDrive klasörü")
        or "My Drive" in i["label"]
        or "Google Drive klasörü" in i["label"]
    )

    return {
        "id": "drive",
        "name": "Google Drive / OneDrive / Dropbox",
        "bytes": total,
        "items": uniq,
        "cleanable": False,
        "clean_actions": [],
        "note": (
            "Drive 'çevrimdışı kullanılabilir' yaklaşık olarak DriveFS content_cache + mirror klasörleridir. "
            f"Tahmini çevrimdışı/yerel kopya: {offlineish / (1024**3):.1f} GB. "
            "Silme yok — Drive/OneDrive ayarlarından yönet."
        ),
        "extra": {"offlineish_bytes": offlineish},
    }


def _scan_temp() -> dict:
    paths = [
        os.environ.get("TEMP", os.path.join(_local(), "Temp")),
        os.environ.get("TMP", ""),
        r"C:\Windows\Temp",
        os.path.join(_local(), "Temp"),
    ]
    paths = [p for p in paths if p]
    # unique
    seen = set()
    uniq = []
    for p in paths:
        ep = os.path.normcase(os.path.abspath(os.path.expandvars(p)))
        if ep not in seen:
            seen.add(ep)
            uniq.append(p)
    return _cat(
        "temp",
        "Geçici dosyalar (Temp)",
        uniq,
        cleanable=True,
        clean_actions=["temp"],
        note="Kullanılmayan temp dosyaları silinir.",
    )


def _scan_recycle() -> dict:
    # Recycle Bin on C:
    paths = [r"C:\$Recycle.Bin"]
    return _cat(
        "recycle",
        "Geri Dönüşüm Kutusu",
        paths,
        cleanable=True,
        clean_actions=["recycle"],
    )


def _scan_package_caches() -> dict:
    u, la = _user(), _local()
    paths = [
        os.path.join(la, "npm-cache"),
        os.path.join(u, ".npm"),
        os.path.join(la, "pip", "Cache"),
        os.path.join(la, "pypoetry", "Cache"),
        os.path.join(u, ".cache", "pip"),
        os.path.join(la, "Yarn", "Cache"),
        os.path.join(u, ".nuget", "packages"),
        os.path.join(la, "NuGet"),
        os.path.join(u, ".gradle", "caches"),
        os.path.join(u, ".m2", "repository"),
        os.path.join(la, "pnpm-store"),
        os.path.join(la, "pnpm"),
    ]
    return _cat(
        "pkg_cache",
        "Paket cache (npm/pip/nuget…)",
        paths,
        cleanable=True,
        clean_actions=["npm_cache", "pip_cache"],
        note="Yeniden indirme gerekir; projeler bozulmaz.",
    )


def _scan_windows() -> dict:
    paths = [
        r"C:\Windows\SoftwareDistribution\Download",
        r"C:\Windows\Temp",
        r"C:\Windows\Logs",
        r"C:\ProgramData\Microsoft\Windows\WER",
        r"C:\Windows\Installer",  # size only — do not clean via app
    ]
    return _cat(
        "windows",
        "Windows (Update / Logs / Installer)",
        paths,
        cleanable=True,
        clean_actions=["windows_update_cache"],
        note="Installer klasörü sadece rapor; Update Download temizlenebilir.",
    )


def _top_appdata() -> dict:
    """Largest folders under Local AppData (depth 1) — prioritize known hogs."""
    root = Path(_local())
    items = []
    priority = {
        "docker", "docker desktop", "packages", "google", "microsoft",
        "temp", "pip", "npm-cache", "yarn", "nuget", "android",
        "bluestacks", "ldplayer", "programs", "nvidia", "steam",
        "epicgameslauncher", "discord", "spotify", "mozilla", "googlechrome",
        "ms-playwright", "pnpm", "pnpm-store", "cursor", "com.docker.docker",
    }
    if root.exists():
        try:
            children = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            children = []

        def rank(p: Path) -> tuple[int, str]:
            name = p.name.lower()
            for i, key in enumerate(priority):
                if key in name:
                    return (i, name)
            return (100, name)

        children = sorted(children, key=rank)[:30]

        def measure(p: Path) -> dict:
            b, e = dir_size(p, timeout=60)
            return {"path": str(p), "label": p.name, "bytes": b, "errors": e, "exists": True}

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(measure, p): p for p in children}
            for fut in as_completed(futs):
                try:
                    items.append(fut.result())
                except Exception:
                    pass
        items.sort(key=lambda x: x["bytes"], reverse=True)
        items = [i for i in items if i["bytes"] > 0][:25]
    total = sum(i["bytes"] for i in items)
    return {
        "id": "appdata_local",
        "name": "AppData\\Local (en büyükler)",
        "bytes": total,
        "items": items,
        "cleanable": False,
        "clean_actions": [],
        "note": "İlk 30 klasör; timeout olanlar 0 görünebilir.",
        "extra": {},
    }


def _scan_wsl() -> dict:
    la = _local()
    paths = [
        os.path.join(la, "Packages"),  # store apps / WSL distros sometimes
        os.path.join(la, "Docker"),
        os.path.join(la, "wsl"),
        r"C:\ProgramData\Docker",
        r"C:\ProgramData\DockerDesktop",
        os.path.join(la, "DockerDesktop"),
    ]
    # Also look for ext4.vhdx
    found = []
    packages = Path(la) / "Packages"
    if packages.exists():
        try:
            for vhdx in packages.rglob("ext4.vhdx"):
                found.append(str(vhdx))
                if len(found) >= 8:
                    break
        except OSError:
            pass
    paths = paths + found
    return _cat(
        "wsl_docker_data",
        "WSL / Docker Desktop data",
        paths,
        cleanable=False,
        note="WSL vhdx ve Docker Desktop verisi — Docker prune tercih et.",
    )


_SCANNERS: list[Callable[[], dict]] = [
    _scan_docker,
    _scan_cursor,
    _scan_bluestacks,
    _scan_ldplayer,
    _scan_drive,
    _scan_temp,
    _scan_recycle,
    _scan_package_caches,
    _scan_windows,
    _top_appdata,
    _scan_wsl,
]


def _persist_scans() -> None:
    with _lock:
        quick = dict(_state["quick"])
        deep = dict(_state["deep"])
    scan_cache.save_cache(
        {
            "quick": {
                "categories": quick.get("categories") or [],
                "finished": quick.get("finished"),
                "status": "done" if quick.get("categories") else quick.get("status"),
            },
            "deep": {
                "roots": deep.get("roots") or [],
                "finished": deep.get("finished"),
                "progress": deep.get("progress", 0),
                "status": deep.get("status"),
            },
        }
    )


def hydrate_from_cache() -> bool:
    """Load last results into memory. Returns True if cache had quick categories."""
    data = scan_cache.load_cache()
    if not data:
        return False
    saved_at = data.get("saved_at")
    q = data.get("quick") or {}
    d = data.get("deep") or {}
    cats = q.get("categories") or []
    with _lock:
        if cats:
            _state["quick"].update(
                {
                    "status": "done",
                    "categories": cats,
                    "started": None,
                    "finished": q.get("finished") or saved_at,
                    "error": None,
                    "from_cache": True,
                    "cached_at": saved_at,
                }
            )
        roots = d.get("roots") or []
        if roots:
            _state["deep"].update(
                {
                    "status": "done" if d.get("status") == "done" else d.get("status") or "done",
                    "roots": roots,
                    "progress": d.get("progress", 100),
                    "current": "",
                    "started": None,
                    "finished": d.get("finished") or saved_at,
                    "error": None,
                    "from_cache": True,
                    "cached_at": saved_at,
                }
            )
    return bool(cats)


def bootstrap_scans(*, force: bool = False) -> dict[str, Any]:
    """
    UX: show cache instantly; only auto-rescan if missing/stale/forced.
    """
    has_cache = hydrate_from_cache()
    data = scan_cache.load_cache()
    need = force or (not has_cache) or scan_cache.is_stale(data)
    if need:
        quick_scan(background=True)
    return get_scan_state()


def _run_quick() -> None:
    _set_quick(
        status="running",
        started=time.time(),
        finished=None,
        error=None,
        from_cache=False,
        cached_at=None,
    )
    # Keep previous categories visible while refreshing if present
    with _lock:
        categories: list[dict] = list(_state["quick"].get("categories") or [])
    try:
        fresh: list[dict] = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(fn) for fn in _SCANNERS]
            for fut in as_completed(futs):
                try:
                    cat = fut.result()
                    # replace same id if refreshing
                    fresh = [c for c in fresh if c.get("id") != cat.get("id")]
                    fresh.append(cat)
                    fresh.sort(key=lambda c: c["bytes"], reverse=True)
                    # merge: prefer fresh ids, keep old others briefly
                    by_id = {c["id"]: c for c in categories if c.get("id")}
                    by_id.update({c["id"]: c for c in fresh})
                    merged = sorted(by_id.values(), key=lambda c: c["bytes"], reverse=True)
                    _set_quick(categories=list(merged))
                except Exception as e:
                    fresh.append(
                        {
                            "id": f"error-{time.time()}",
                            "name": f"Hata: {e}",
                            "bytes": 0,
                            "items": [],
                            "cleanable": False,
                            "clean_actions": [],
                            "note": str(e),
                            "extra": {},
                        }
                    )
                    _set_quick(categories=list(fresh))
        fresh.sort(key=lambda c: c["bytes"], reverse=True)
        finished = time.time()
        _set_quick(
            status="done",
            finished=finished,
            categories=fresh,
            from_cache=False,
            cached_at=finished,
        )
        _persist_scans()
    except Exception as e:
        _set_quick(status="error", error=str(e), finished=time.time())


def quick_scan(background: bool = True) -> dict[str, Any]:
    with _lock:
        st = _state["quick"]["status"]
        if st == "running":
            return get_scan_state()
    if background:
        threading.Thread(target=_run_quick, daemon=True).start()
        return get_scan_state()
    _run_quick()
    return get_scan_state()


def _run_deep(max_depth: int = 2, min_bytes: int = 500 * 1024 * 1024) -> None:
    """Scan C:\\ top-level and expand large dirs up to max_depth."""
    _set_deep(
        status="running",
        progress=0,
        current="",
        started=time.time(),
        finished=None,
        error=None,
        from_cache=False,
        cached_at=None,
    )
    # keep previous roots visible
    root = Path("C:\\")
    try:
        try:
            children = sorted(
                [p for p in root.iterdir() if p.exists()],
                key=lambda p: p.name.lower(),
            )
        except OSError as e:
            _set_deep(status="error", error=str(e), finished=time.time())
            return

        total_steps = max(len(children), 1)
        results: list[dict] = []

        for i, child in enumerate(children):
            _set_deep(progress=int((i / total_steps) * 100), current=str(child))
            name = child.name.lower()
            if name in {"$recycle.bin", "system volume information"}:
                pass
            try:
                if child.is_file():
                    b, e = dir_size(child)
                    results.append(
                        {
                            "path": str(child),
                            "label": child.name,
                            "bytes": b,
                            "errors": e,
                            "children": [],
                        }
                    )
                elif child.is_dir():
                    b, e = dir_size(child)
                    node = {
                        "path": str(child),
                        "label": child.name,
                        "bytes": b,
                        "errors": e,
                        "children": [],
                    }
                    if b >= min_bytes and max_depth >= 1:
                        node["children"] = _expand_large(child, depth=1, max_depth=max_depth, min_bytes=min_bytes)
                    results.append(node)
            except OSError:
                continue
            results.sort(key=lambda x: x["bytes"], reverse=True)
            _set_deep(roots=list(results), progress=int(((i + 1) / total_steps) * 100))

        finished = time.time()
        _set_deep(
            status="done",
            progress=100,
            current="",
            finished=finished,
            roots=results,
            from_cache=False,
            cached_at=finished,
        )
        _persist_scans()
    except Exception as e:
        _set_deep(status="error", error=str(e), finished=time.time())


def _expand_large(path: Path, depth: int, max_depth: int, min_bytes: int) -> list[dict]:
    kids = []
    try:
        entries = list(path.iterdir())
    except OSError:
        return kids
    dirs = []
    for p in entries:
        try:
            if p.is_symlink():
                continue
            if p.is_dir():
                dirs.append(p)
        except OSError:
            continue
    dirs = dirs[:40]
    measured = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(dir_size, d): d for d in dirs}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                b, e = fut.result()
            except Exception:
                continue
            node = {"path": str(d), "label": d.name, "bytes": b, "errors": e, "children": []}
            if b >= min_bytes and depth < max_depth:
                node["children"] = _expand_large(d, depth + 1, max_depth, min_bytes)
            measured.append(node)
    measured.sort(key=lambda x: x["bytes"], reverse=True)
    return measured[:20]


def start_deep_scan(background: bool = True) -> dict[str, Any]:
    with _lock:
        if _state["deep"]["status"] == "running":
            return get_scan_state()
    if background:
        threading.Thread(target=_run_deep, daemon=True).start()
        return get_scan_state()
    _run_deep()
    return get_scan_state()
