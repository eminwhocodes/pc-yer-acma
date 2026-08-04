"""Safe cleanup actions."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from applog import log, log_exception


def _run(args: list[str], timeout: int = 300) -> dict[str, Any]:
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "ok": r.returncode == 0,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
            "code": r.returncode,
            "cmd": args,
        }
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": f"Komut yok: {args[0]}", "code": -1, "cmd": args}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "Zaman aşımı", "code": -1, "cmd": args}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "code": -1, "cmd": args}


def _clear_dir_contents(path: str, keep_names: set[str] | None = None) -> dict[str, Any]:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return {"ok": True, "deleted": 0, "freed_hint": 0, "errors": [f"Yok: {path}"]}
    keep_names = keep_names or set()
    deleted = 0
    errors: list[str] = []
    for child in list(root.iterdir()):
        if child.name in keep_names:
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=False)
            else:
                child.unlink(missing_ok=True)
            deleted += 1
        except Exception as e:
            errors.append(f"{child}: {e}")
    return {"ok": len(errors) == 0, "deleted": deleted, "errors": errors[:30]}


def _find_docker_vhdx() -> str | None:
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") or ""
    user = os.environ.get("USERPROFILE") or ""
    username = os.environ.get("USERNAME") or ""
    for base in (
        Path(local) / "Docker" if local else None,
        Path(user) / "AppData" / "Local" / "Docker" if user else None,
        Path(rf"C:\Users\{username}\AppData\Local\Docker") if username else None,
    ):
        if not base:
            continue
        direct = base / "wsl" / "disk" / "docker_data.vhdx"
        if direct.exists():
            return str(direct)
        try:
            found = list(base.rglob("docker_data.vhdx"))
            if found:
                return str(found[0])
        except OSError:
            continue
    return None


def _diskpart_compact(vhdx: str) -> dict[str, Any]:
    vhdx_esc = vhdx.replace('"', '""')
    dp_script = "\n".join(
        [
            f'select vdisk file="{vhdx_esc}"',
            "attach vdisk readonly",
            "compact vdisk",
            "detach vdisk",
            "exit",
        ]
    )
    tf = Path(os.environ.get("TEMP", ".")) / f"pc-yer-acma-compact-{os.getpid()}.txt"
    try:
        tf.write_text(dp_script, encoding="ascii", errors="replace")
        out = _run(["diskpart", "/s", str(tf)], timeout=900)
    finally:
        try:
            tf.unlink(missing_ok=True)
        except Exception:
            pass
    text = f"{out.get('stdout','')}\n{out.get('stderr','')}".lower()
    # diskpart often returns 0 even with "access denied"
    failed_hints = (
        "access is denied",
        "erişim engellendi",
        "error",
        "failed",
        "not found",
        "cannot",
        "bulunamadı",
    )
    success_hints = ("completed successfully", "successfully", "başarıyla")
    looks_fail = any(h in text for h in failed_hints) and not any(h in text for h in success_hints)
    if out.get("ok") and looks_fail:
        out["ok"] = False
    out["method"] = "diskpart"
    return out


def _optimize_vhd(vhdx: str) -> dict[str, Any]:
    ps = (
        "$ErrorActionPreference='Stop'; "
        f"$p = '{vhdx.replace(chr(39), chr(39)+chr(39))}'; "
        "Import-Module Hyper-V -ErrorAction Stop; "
        "Optimize-VHD -Path $p -Mode Full; "
        "'OK'"
    )
    out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=900)
    out["method"] = "optimize-vhd"
    return out


def compact_docker_vhdx() -> dict[str, Any]:
    """Compact docker_data.vhdx. Works best when Docker Desktop is fully stopped."""
    result: dict[str, Any] = {
        "ok": False,
        "action": "docker_compact_vhdx",
        "title": "Docker VHDX küçült (WSL disk)",
        "details": [],
        "freed_bytes": 0,
    }
    vhdx = _find_docker_vhdx()
    if not vhdx:
        result["error"] = "docker_data.vhdx bulunamadı"
        log("vhdx_not_found", level="ERROR")
        return result

    before = Path(vhdx).stat().st_size
    result["details"].append({"step": "before", "path": vhdx, "bytes": before})
    log("vhdx_compact_start", path=vhdx, before_bytes=before)

    # Stop Docker / WSL so file unlocks — compact DOES work without Docker running
    # (in fact it requires Docker to be stopped)
    result["details"].append(
        {
            "step": "stop_docker_processes",
            **_run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-Process 'Docker Desktop','com.docker.backend','com.docker.build',"
                    "'com.docker.service' -ErrorAction SilentlyContinue | "
                    "Stop-Process -Force -ErrorAction SilentlyContinue; "
                    "Stop-Service com.docker.service -Force -ErrorAction SilentlyContinue",
                ],
                timeout=90,
            ),
        }
    )
    result["details"].append({"step": "wsl_shutdown", **_run(["wsl", "--shutdown"], timeout=120)})
    time.sleep(5)

    # Try Optimize-VHD first, then diskpart
    compact = _optimize_vhd(vhdx)
    result["details"].append({"step": "compact_optimize", **compact})
    if not compact.get("ok"):
        compact2 = _diskpart_compact(vhdx)
        result["details"].append({"step": "compact_diskpart", **compact2})
        compact = compact2

    after = Path(vhdx).stat().st_size if Path(vhdx).exists() else 0
    freed = max(0, before - after)
    size_info = {
        "step": "size",
        "before_bytes": before,
        "after_bytes": after,
        "freed_bytes": freed,
        "before_gb": round(before / (1024**3), 2),
        "after_gb": round(after / (1024**3), 2),
        "freed_gb": round(freed / (1024**3), 2),
    }
    result["details"].append(size_info)
    result["freed_bytes"] = freed

    # Do NOT auto-restart Docker — user said they're updating/reopening manually.
    # Starting mid-update made "Starting..." hang before.
    result["details"].append(
        {
            "step": "note",
            "text": "Docker otomatik başlatılmadı. Güncelleme/açma işini sen yap.",
        }
    )

    if freed > 0:
        result["ok"] = True
        result["note"] = (
            f"VHDX küçüldü: {size_info['before_gb']} GB → {size_info['after_gb']} GB "
            f"(~{size_info['freed_gb']} GB boşaldı). Docker'ı elle aç."
        )
    elif compact.get("ok"):
        result["ok"] = False
        result["error"] = (
            "Compact komutu bitti ama dosya boyutu değişmedi (0 byte). "
            "Dosya kilitli kalmış olabilir veya içeride sıkıştırılacak boşluk yok. "
            "Docker tamamen kapalıyken tekrar dene; loglara bak."
        )
        result["note"] = result["error"]
    else:
        result["ok"] = False
        result["error"] = compact.get("stderr") or compact.get("stdout") or "Compact başarısız"
        result["note"] = result["error"]

    log(
        "vhdx_compact_end",
        level="INFO" if result["ok"] else "WARN",
        path=vhdx,
        before_bytes=before,
        after_bytes=after,
        freed_bytes=freed,
        ok=result["ok"],
        error=result.get("error"),
        details=result["details"],
    )
    try:
        from scanner import docker_scan

        docker_scan._details_cache = None
    except Exception:
        pass
    return result


ACTIONS = {
    "temp": {
        "title": "Temp klasörlerini temizle",
        "risk": "low",
        "desc": "Kullanıcı ve Windows Temp içeriği",
    },
    "recycle": {
        "title": "Geri Dönüşüm Kutusunu boşalt",
        "risk": "low",
        "desc": "C: Geri Dönüşüm Kutusu",
    },
    "docker_containers": {
        "title": "Durmuş Docker container'ları sil",
        "risk": "low",
        "desc": "docker container prune -f",
    },
    "docker_dangling_images": {
        "title": "Dangling Docker image'leri sil",
        "risk": "low",
        "desc": "docker image prune -f",
    },
    "docker_unused_images": {
        "title": "Kullanılmayan tüm Docker image'leri sil",
        "risk": "medium",
        "desc": "docker image prune -a -f (yeniden pull gerekir)",
    },
    "docker_volumes": {
        "title": "Kullanılmayan Docker volume'leri sil",
        "risk": "medium",
        "desc": "docker volume prune -f (data kaybı olabilir)",
    },
    "docker_build_cache": {
        "title": "Docker build cache temizle",
        "risk": "low",
        "desc": "docker builder prune -af",
    },
    "docker_system_prune": {
        "title": "Docker system prune (agresif)",
        "risk": "medium",
        "desc": "docker system prune -af --volumes",
    },
    "docker_compact_vhdx": {
        "title": "Docker VHDX küçült (WSL disk)",
        "risk": "medium",
        "desc": "Docker KAPALIYKEN en iyi çalışır. wsl --shutdown + Optimize-VHD/diskpart. Yer açılmazsa log'a yazılır.",
    },
    "cursor_cache": {
        "title": "Cursor cache temizle",
        "risk": "low",
        "desc": "Cache/GPUCache/Code Cache — ayarlar durur",
    },
    "npm_cache": {
        "title": "npm cache temizle",
        "risk": "low",
        "desc": "npm cache clean --force",
    },
    "pip_cache": {
        "title": "pip cache temizle",
        "risk": "low",
        "desc": "pip cache purge",
    },
    "windows_update_cache": {
        "title": "Windows Update Download cache",
        "risk": "medium",
        "desc": "SoftwareDistribution\\Download (yönetici gerekebilir)",
    },
}


def list_actions() -> list[dict]:
    return [{"id": k, **v} for k, v in ACTIONS.items()]


def run_action(action_id: str) -> dict[str, Any]:
    if action_id not in ACTIONS:
        return {"ok": False, "action": action_id, "error": "Bilinmeyen aksiyon"}

    log("action_start", action=action_id)
    meta = ACTIONS[action_id]
    result: dict[str, Any] = {"ok": False, "action": action_id, "title": meta["title"], "details": []}

    try:
        if action_id == "docker_compact_vhdx":
            result = compact_docker_vhdx()
            log("action_end", action=action_id, ok=result.get("ok"), freed_bytes=result.get("freed_bytes"))
            return result

        if action_id == "temp":
            paths = []
            for key in ("TEMP", "TMP"):
                v = os.environ.get(key)
                if v:
                    paths.append(v)
            paths.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp"))
            paths.append(r"C:\Windows\Temp")
            seen = set()
            for p in paths:
                if not p:
                    continue
                norm = os.path.normcase(os.path.abspath(p))
                if norm in seen:
                    continue
                seen.add(norm)
                result["details"].append({"path": p, **_clear_dir_contents(p)})
            result["ok"] = True

        elif action_id == "recycle":
            r = _run(
                ["powershell", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"]
            )
            result["ok"] = True
            result["details"].append(r)

        elif action_id == "docker_containers":
            r = _run(["docker", "container", "prune", "-f"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "docker_dangling_images":
            r = _run(["docker", "image", "prune", "-f"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "docker_unused_images":
            r = _run(["docker", "image", "prune", "-a", "-f"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "docker_volumes":
            r = _run(["docker", "volume", "prune", "-f"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "docker_build_cache":
            r = _run(["docker", "builder", "prune", "-af"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "docker_system_prune":
            r = _run(["docker", "system", "prune", "-af", "--volumes"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "cursor_cache":
            local = os.environ.get("LOCALAPPDATA", "")
            user = os.environ.get("USERPROFILE", "")
            targets = [
                os.path.join(local, "Cursor", "Cache"),
                os.path.join(local, "Cursor", "Code Cache"),
                os.path.join(local, "Cursor", "GPUCache"),
                os.path.join(local, "Cursor", "CachedData"),
                os.path.join(local, "Cursor", "CachedExtensionVSIXs"),
                os.path.join(local, "Cursor", "logs"),
                os.path.join(user, ".cursor", "ai-tracking"),
            ]
            for t in targets:
                if Path(t).exists():
                    result["details"].append({"path": t, **_clear_dir_contents(t)})
            result["ok"] = True

        elif action_id == "npm_cache":
            r = _run(["npm", "cache", "clean", "--force"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "pip_cache":
            r = _run(["pip", "cache", "purge"])
            if not r["ok"]:
                r = _run(["python", "-m", "pip", "cache", "purge"])
            result["ok"] = r["ok"]
            result["details"].append(r)

        elif action_id == "windows_update_cache":
            path = r"C:\Windows\SoftwareDistribution\Download"
            stop = _run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Stop-Service wuauserv,bits,cryptsvc,msiserver -Force -ErrorAction SilentlyContinue; "
                    "Start-Sleep -Seconds 1",
                ]
            )
            result["details"].append({"step": "stop_services", **stop})
            cleared = _clear_dir_contents(path)
            result["details"].append({"path": path, **cleared})
            start = _run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Start-Service bits,cryptsvc,wuauserv -ErrorAction SilentlyContinue",
                ]
            )
            result["details"].append({"step": "start_services", **start})
            result["ok"] = cleared.get("ok", False) or cleared.get("deleted", 0) > 0
            if not result["ok"] and cleared.get("errors"):
                result["error"] = "Erişim engellendi — uygulamayı yönetici olarak çalıştırın"

        else:
            result["error"] = "Aksiyon uygulanamadı"

    except Exception as e:
        log_exception("action_failed", e, action=action_id)
        result["ok"] = False
        result["error"] = str(e)

    log("action_end", action=action_id, ok=result.get("ok"), error=result.get("error"))
    return result
