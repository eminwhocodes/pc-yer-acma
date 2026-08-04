"""Docker size and inventory via docker CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any


def _run(args: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode == 0, out.strip()
    except FileNotFoundError:
        return False, "docker bulunamadı"
    except subprocess.TimeoutExpired:
        return False, "docker komutu zaman aşımı"
    except Exception as e:
        return False, str(e)


def docker_available() -> bool:
    return shutil.which("docker") is not None


_daemon_cache: dict[str, Any] | None = None
_daemon_cache_at = 0.0
_DAEMON_TTL = 20.0


def docker_daemon_status(*, fresh: bool = False) -> dict[str, Any]:
    """CLI installed vs daemon/Desktop running (cached briefly)."""
    global _daemon_cache, _daemon_cache_at
    now = time.time()
    if (
        not fresh
        and _daemon_cache is not None
        and (now - _daemon_cache_at) < _DAEMON_TTL
    ):
        return dict(_daemon_cache)

    if not docker_available():
        result = {
            "cli": False,
            "running": False,
            "available": False,
            "message": "Docker CLI yüklü değil",
            "hint": "Docker Desktop kurulu mu kontrol et.",
            "detail": None,
        }
        _daemon_cache, _daemon_cache_at = result, now
        return dict(result)

    ok, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8)
    if ok and out and "error" not in out.lower():
        result = {
            "cli": True,
            "running": True,
            "available": True,
            "message": None,
            "hint": None,
            "version": out.splitlines()[0].strip(),
            "detail": None,
        }
        _daemon_cache, _daemon_cache_at = result, now
        return dict(result)

    detail = out or ""
    lower = detail.lower()
    if "dockerdesktop" in lower or "pipe" in lower or "cannot connect" in lower or "npipe" in lower:
        msg = "Docker Desktop kapalı veya daemon çalışmıyor"
        hint = "Docker Desktop'ı aç, yeşil 'Engine running' olunca Yenile'ye bas."
    elif "permission" in lower or "access is denied" in lower:
        msg = "Docker'a erişim reddedildi"
        hint = "Docker Desktop açık mı ve bu kullanıcıyla çalışıyor mu bak."
    else:
        msg = "Docker daemon yanıt vermiyor"
        hint = "Docker Desktop'ı başlatıp tekrar dene."
    result = {
        "cli": True,
        "running": False,
        "available": False,
        "message": msg,
        "hint": hint,
        "detail": detail[:500],
    }
    _daemon_cache, _daemon_cache_at = result, now
    return dict(result)


def _parse_size_to_bytes(s: str) -> int:
    """Parse docker size strings like '1.2GB', '350MB'."""
    if not s:
        return 0
    s = s.strip().upper().replace(" ", "")
    units = [
        ("KIB", 1024),
        ("MIB", 1024**2),
        ("GIB", 1024**3),
        ("TIB", 1024**4),
        ("KB", 1000),
        ("MB", 1000**2),
        ("GB", 1000**3),
        ("TB", 1000**4),
        ("B", 1),
    ]
    for suffix, mult in units:
        if s.endswith(suffix):
            num = s[: -len(suffix)]
            try:
                return int(float(num) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def docker_system_df() -> dict[str, Any]:
    status = docker_daemon_status(fresh=True)
    if not status.get("cli"):
        return {
            "available": False,
            "daemon": status,
            "error": status["message"],
            "total_bytes": 0,
            "types": [],
        }
    if not status.get("running"):
        return {
            "available": False,
            "daemon": status,
            "error": status["message"],
            "hint": status.get("hint"),
            "total_bytes": 0,
            "types": [],
        }

    ok, out = _run(["docker", "system", "df", "--format", "{{json .}}"])
    if not ok:
        ok2, out2 = _run(["docker", "system", "df"])
        return {
            "available": ok2,
            "daemon": status,
            "error": None if ok2 else (out or status.get("message")),
            "raw": out2 if ok2 else out,
            "total_bytes": 0,
            "types": [],
        }

    types = []
    total = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Type, TotalCount, Active, Size, Reclaimable
        size_s = str(row.get("Size", "0"))
        reclaim_s = str(row.get("Reclaimable", "0")).split("(")[0].strip()
        size_b = _parse_size_to_bytes(size_s)
        reclaim_b = _parse_size_to_bytes(reclaim_s)
        total += size_b
        types.append(
            {
                "type": row.get("Type", "?"),
                "total_count": row.get("TotalCount", row.get("Total", 0)),
                "active": row.get("Active", 0),
                "size": size_s,
                "size_bytes": size_b,
                "reclaimable": row.get("Reclaimable", ""),
                "reclaimable_bytes": reclaim_b,
            }
        )
    return {"available": True, "daemon": status, "error": None, "total_bytes": total, "types": types}


def _json_lines(args: list[str]) -> list[dict]:
    ok, out = _run(args)
    if not ok or not out:
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


_details_cache: dict[str, Any] | None = None
_details_cache_at = 0.0
_DETAILS_TTL = 15.0


def docker_disk_footprint() -> dict[str, Any]:
    """Windows Docker Desktop disk usage (VHDX) vs engine-reported size."""
    import os
    from pathlib import Path

    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") or ""
    userprofile = os.environ.get("USERPROFILE") or ""
    username = os.environ.get("USERNAME") or ""

    if local:
        candidates.append(Path(local) / "Docker")
    if userprofile:
        candidates.append(Path(userprofile) / "AppData" / "Local" / "Docker")
    # Elevated process bazen farklı AppData görür; gerçek kullanıcı klasörünü de dene
    if username and username.lower() not in {"system", "local service", "network service"}:
        candidates.append(Path(rf"C:\Users\{username}") / "AppData" / "Local" / "Docker")
    # Yaygın sabit yol
    candidates.append(Path(r"C:\Users") / (username or "Public") / "AppData" / "Local" / "Docker")

    # Unique existing bases
    bases: list[Path] = []
    seen: set[str] = set()
    for c in candidates:
        try:
            key = str(c.resolve()) if c.exists() else str(c)
        except OSError:
            key = str(c)
        if key.lower() in seen:
            continue
        seen.add(key.lower())
        if c.exists():
            bases.append(c)

    vhdx_files: list[dict[str, Any]] = []
    disk_bytes = 0
    checked: list[str] = []

    # Önce bilinen dosyayı doğrudan ölç (rglob'a gerek yok)
    direct_names = [
        Path("wsl") / "disk" / "docker_data.vhdx",
        Path("wsl") / "data" / "ext4.vhdx",
        Path("wsl") / "main" / "ext4.vhdx",
    ]
    for base in bases:
        checked.append(str(base))
        for rel in direct_names:
            p = base / rel
            if not p.exists():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            disk_bytes += size
            vhdx_files.append({"path": str(p), "bytes": size, "label": p.name})

        # Ek vhdx'ler
        try:
            for p in base.rglob("*.vhdx"):
                sp = str(p)
                if any(v["path"] == sp for v in vhdx_files):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                disk_bytes += size
                vhdx_files.append({"path": sp, "bytes": size, "label": p.name})
        except OSError:
            pass

    vhdx_files.sort(key=lambda x: x["bytes"], reverse=True)
    primary = next(
        (v for v in vhdx_files if "docker_data" in v["label"].lower()),
        vhdx_files[0] if vhdx_files else None,
    )
    return {
        "path": checked[0] if checked else None,
        "checked_paths": checked,
        "disk_bytes": disk_bytes,
        "vhdx": vhdx_files[:10],
        "primary_vhdx": primary,
        "found": bool(vhdx_files),
    }


def docker_details(*, fresh: bool = False) -> dict[str, Any]:
    global _details_cache, _details_cache_at
    now = time.time()
    if (
        not fresh
        and _details_cache is not None
        and (now - _details_cache_at) < _DETAILS_TTL
    ):
        return dict(_details_cache)

    df = docker_system_df()
    footprint = docker_disk_footprint()
    engine_bytes = int(df.get("total_bytes") or 0)
    disk_bytes = int(footprint.get("disk_bytes") or 0)
    gap = max(0, disk_bytes - engine_bytes)

    if not df.get("available"):
        result = {
            **df,
            "images": [],
            "containers": [],
            "volumes": [],
            "footprint": footprint,
            "engine_bytes": engine_bytes,
            "gap_bytes": gap,
            "gap_note": (
                "Diskteki Docker VHDX, engine içeriğinden büyük olabilir; "
                "silinen image'ler sonrası VHDX otomatik küçülmez."
            ),
        }
        _details_cache, _details_cache_at = result, now
        return dict(result)

    images = _json_lines(
        [
            "docker",
            "images",
            "--format",
            '{"id":"{{.ID}}","repo":"{{.Repository}}","tag":"{{.Tag}}","size":"{{.Size}}","created":"{{.CreatedSince}}"}',
        ]
    )
    for img in images:
        img["size_bytes"] = _parse_size_to_bytes(str(img.get("size", "")))

    containers = _json_lines(
        [
            "docker",
            "ps",
            "-a",
            "--format",
            '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}","size":"{{.Size}}"}',
        ]
    )
    volumes = _json_lines(
        [
            "docker",
            "volume",
            "ls",
            "--format",
            '{"name":"{{.Name}}","driver":"{{.Driver}}"}',
        ]
    )

    result = {
        **df,
        "images": sorted(images, key=lambda x: x.get("size_bytes", 0), reverse=True)[:100],
        "containers": containers[:100],
        "volumes": volumes[:100],
        "footprint": footprint,
        "engine_bytes": engine_bytes,
        "gap_bytes": gap,
        "gap_note": (
            "AppData\\Local\\Docker altındaki VHDX Windows'ta görünen boyut; "
            "docker system df ise container engine içindeki veri. "
            "Fark çoğunlukla şişmiş docker_data.vhdx (prune sonrası otomatik küçülmez)."
        ),
    }
    _details_cache, _details_cache_at = result, now
    return dict(result)
