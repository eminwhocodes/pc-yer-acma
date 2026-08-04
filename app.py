#!/usr/bin/env python3
"""PC Yer Açma — yerel disk analiz + güvenli temizlik UI."""
from __future__ import annotations

import argparse
import ctypes
import json
import socket
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
sys.path.insert(0, str(ROOT))

from cleaner import list_actions  # noqa: E402
from job_queue import (  # noqa: E402
    cancel_job,
    clear_finished,
    enqueue,
    get_job,
    get_queue,
)
from scanner import (  # noqa: E402
    docker_details,
    get_disk_summary,
    get_scan_state,
    quick_scan,
    start_deep_scan,
    start_workspace_scan,
)
from scanner.categories import bootstrap_scans  # noqa: E402
from scanner.docker_scan import docker_daemon_status  # noqa: E402
from applog import log  # noqa: E402
import datetime as _dt  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765

# Tarayici istegi iptal edince Windows'ta bu hatalar normal
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ensure_admin(extra_args: list[str] | None = None, *, hidden: bool = False) -> None:
    if is_admin():
        return
    script = str(Path(__file__).resolve())
    args = extra_args or []
    params = " ".join([f'"{script}"'] + [f'"{a}"' if " " in a else a for a in args])
    show = 0 if hidden else 1  # SW_HIDE / SW_SHOWNORMAL
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, str(ROOT), show
    )
    if rc <= 32:
        print("Yonetici izni reddedildi veya basarisiz.", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[http] " + (fmt % args) + "\n")

    def log_error(self, fmt: str, *args) -> None:
        msg = fmt % args if args else str(fmt)
        # Client disconnect spam'ini sustur
        if any(x in msg for x in ("10053", "10054", "Broken pipe", "Connection aborted", "Connection reset")):
            return
        super().log_error(fmt, *args)

    def handle(self) -> None:
        try:
            super().handle()
        except _CLIENT_GONE:
            pass
        except Exception as e:
            # Bazen socket hatalari baska tipte gelir
            if isinstance(getattr(e, "__cause__", None), _CLIENT_GONE):
                return
            winerr = getattr(e, "winerror", None)
            if winerr in (10053, 10054, 10038):
                return
            raise

    def _json(self, data, status: int = 200) -> None:
        try:
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            return
        except OSError as e:
            if getattr(e, "winerror", None) in (10053, 10054):
                return
            raise

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        try:
            self._do_GET()
        except _CLIENT_GONE:
            return
        except OSError as e:
            if getattr(e, "winerror", None) in (10053, 10054):
                return
            raise

    def _do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/disk":
            return self._json(get_disk_summary())
        if path == "/api/status":
            state = get_scan_state()
            state["admin"] = is_admin()
            state["queue"] = get_queue()
            return self._json(state)
        if path == "/api/queue":
            return self._json(get_queue())
        if path.startswith("/api/queue/") and path.count("/") == 3:
            job_id = path.rsplit("/", 1)[-1]
            job = get_job(job_id)
            if not job:
                return self._json({"ok": False, "error": "job yok"}, 404)
            return self._json(job)
        if path == "/api/docker/status":
            return self._json(docker_daemon_status())
        if path == "/api/scan/quick":
            return self._json(quick_scan(background=True))
        if path == "/api/scan/deep":
            return self._json(start_deep_scan(background=True))
        if path == "/api/scan/workspaces":
            return self._json({"workspaces": start_workspace_scan(background=True)})
        if path == "/api/docker":
            from urllib.parse import parse_qs

            qs = parse_qs(urlparse(self.path).query)
            fresh = (qs.get("fresh") or ["0"])[0] in ("1", "true", "yes")
            return self._json(docker_details(fresh=fresh))
        if path == "/api/actions":
            return self._json({"actions": list_actions(), "admin": is_admin()})
        if path == "/api/scan/bootstrap":
            return self._json(bootstrap_scans(force=False))
        if path == "/api/logs":
            log_dir = ROOT / "logs"
            files = []
            if log_dir.exists():
                for p in sorted(log_dir.glob("*.log"), reverse=True)[:14]:
                    try:
                        files.append(
                            {
                                "name": p.name,
                                "bytes": p.stat().st_size,
                                "mtime": p.stat().st_mtime,
                            }
                        )
                    except OSError:
                        continue
            # last 80 lines of today
            today = log_dir / f"{_dt.datetime.now():%Y-%m-%d}.log"
            lines = []
            if today.exists():
                try:
                    lines = today.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
                except OSError:
                    pass
            return self._json({"files": files, "tail": lines, "dir": str(log_dir)})
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        return super().do_GET()

    def do_POST(self) -> None:
        try:
            self._do_POST()
        except _CLIENT_GONE:
            return
        except OSError as e:
            if getattr(e, "winerror", None) in (10053, 10054):
                return
            raise

    def _do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/scan/bootstrap":
            payload = self._read_json()
            return self._json(bootstrap_scans(force=bool(payload.get("force"))))
        if path == "/api/clean":
            payload = self._read_json()
            action = payload.get("action")
            confirm = payload.get("confirm")
            if not action:
                return self._json({"ok": False, "error": "action gerekli"}, 400)
            if not confirm:
                return self._json({"ok": False, "error": "confirm: true gerekli"}, 400)
            return self._json(enqueue(action))
        if path == "/api/queue/cancel":
            payload = self._read_json()
            return self._json(cancel_job(str(payload.get("id", ""))))
        if path == "/api/queue/clear":
            return self._json(clear_finished())
        return self._json({"ok": False, "error": "not found"}, 404)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PC Yer Acma")
    p.add_argument(
        "--tray",
        action="store_true",
        default=True,
        help="System tray modunda calis (varsayilan)",
    )
    p.add_argument(
        "--no-tray",
        action="store_true",
        help="Klasik konsol modu (tray yok)",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Baslangicta tarayici acma",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    use_tray = not args.no_tray
    argv_extra = []
    if use_tray:
        argv_extra.append("--tray")
    if args.no_browser:
        argv_extra.append("--no-browser")
    if args.no_tray:
        argv_extra = ["--no-tray"] + (["--no-browser"] if args.no_browser else [])

    ensure_admin(argv_extra, hidden=use_tray)

    if use_tray:
        try:
            from tray_ui import hide_console

            hide_console()
        except Exception:
            pass

    if not STATIC.exists():
        print("static/ eksik", file=sys.stderr)
        sys.exit(1)

    url = f"http://{HOST}:{PORT}/"

    if not port_free(HOST, PORT):
        print(f"Port {PORT} dolu — mevcut ornek aciliyor.")
        webbrowser.open(url)
        if use_tray:
            # Already running; nothing to tray
            sys.exit(0)
        sys.exit(0)

    bootstrap_scans(force=False)
    log("app_start", admin=is_admin(), url=url)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)

    def serve() -> None:
        httpd.serve_forever()

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()

    print(f"PC Yer Acma (ADMIN) -> {url}")
    if use_tray:
        print("System tray aktif. Sag tik: tarayici / konsol / cikis")
    else:
        print("Durdurmak icin Ctrl+C")

    if not args.no_browser and not use_tray:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    if use_tray:
        from tray_ui import hide_console, run_tray

        hide_console()

        def on_exit() -> None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
            # Force exit — tray/daemon threads can hang otherwise
            ctypes.windll.kernel32.ExitProcess(0)

        try:
            run_tray(url=url, on_exit=on_exit)
        except Exception as e:
            print(f"Tray basarisiz ({e}), konsol moduna dusuluyor.")
            print("Durdurmak icin Ctrl+C")
            try:
                server_thread.join()
            except KeyboardInterrupt:
                print("\nKapatiliyor...")
                httpd.shutdown()
    else:
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            print("\nKapatiliyor...")
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
