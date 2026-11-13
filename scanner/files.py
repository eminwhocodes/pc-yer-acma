"""Kullanıcı klasörlerinde büyük, gereksiz olabilecek ve kopya dosyaları bulur.

- Büyük dosyalar: eşik üstü (varsayılan 100 MB).
- Kopyalar: içerik aynı, ad/konum farklı olabilir. Önce boyut, sonra
  baş+son 64 KB, sonra tam BLAKE2b hash. Sabit bağlantılar (aynı dosya) sayılmaz.
- Gereksiz olabilir: indirilen kurulum dosyaları, açılmış arşivler, yarım
  indirmeler, uzun süredir dokunulmamış indirmeler, "Kopya"/"(1)" adlılar.
- Bulutta duran (indirilmemiş) OneDrive/Drive dosyaları okunmaz; okumak onları indirir.

Silme yok: yalnız Geri Dönüşüm Kutusu'na taşıma (bkz. enqueue_trash).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat as _stat
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RESULT_FILE = DATA_DIR / "files_scan.json"
HASH_CACHE_FILE = DATA_DIR / "hash_cache.json"

HOME = Path(os.path.expanduser("~"))

BIG_BYTES = 100 * 1024 * 1024
DUP_MIN_BYTES = 256 * 1024
OLD_DAYS = 365
OLD_MIN_BYTES = 10 * 1024 * 1024
ARCHIVE_OLD_DAYS = 30
PARTIAL_HEAD = 64 * 1024
MAX_LIST = 400

SKIP_DIRS = {
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    ".nuxt",
    ".gradle",
    ".idea",
    "$recycle.bin",
    "system volume information",
    "appdata",
}

INSTALLER_EXT = {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle", ".iso", ".img", ".dmg", ".apk", ".xapk", ".pkg", ".deb", ".rpm"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst"}
PARTIAL_EXT = {".crdownload", ".part", ".partial", ".download", ".tmp", ".opdownload"}
COPY_NAME_RE = re.compile(
    r"(\s-\s(kopya|copy)(\s\(\d+\))?$)|(\s\(\d+\)$)|(^(kopya|copy of)\s)|(_copy\d*$)",
    re.IGNORECASE,
)

# Windows dosya öznitelikleri (bulut yer tutucuları)
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ATTRS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


def _known_folder(name: str, fallback: Path) -> Path:
    """Gerçek kabuk klasörü (OneDrive yönlendirmesi dahil)."""
    guids = {
        "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    }
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as k:
            key = guids.get(name, name)
            val, _ = winreg.QueryValueEx(k, key)
            if val:
                return Path(os.path.expandvars(val))
    except Exception:
        pass
    return fallback


def default_roots() -> list[dict[str, Any]]:
    rows = [
        ("Masaüstü", _known_folder("Desktop", HOME / "Desktop"), True),
        ("İndirilenler", _known_folder("Downloads", HOME / "Downloads"), True),
        ("Belgeler", _known_folder("Personal", HOME / "Documents"), True),
        ("Resimler", _known_folder("My Pictures", HOME / "Pictures"), True),
        ("Videolar", _known_folder("My Video", HOME / "Videos"), True),
        ("Müzik", _known_folder("My Music", HOME / "Music"), True),
        ("OneDrive", HOME / "OneDrive", False),
        ("YandexDisk", HOME / "YandexDisk", False),
    ]
    out = []
    seen = set()
    for label, p, on in rows:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "path": str(p), "exists": p.exists(), "default": on})
    return out


# ---------------------------------------------------------------- durum

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "phase": "",
    "progress": 0,
    "current": "",
    "started": None,
    "finished": None,
    "error": None,
    "roots": [],
    "stats": {},
    "big": [],
    "duplicates": [],
    "folder_dups": [],
    "junk": [],
    "trashed": [],
    "rev": 0,
}
_known_paths: set[str] = set()


def _set(**kw: Any) -> None:
    with _lock:
        _state.update(kw)


def get_files_state(full: bool = True) -> dict[str, Any]:
    with _lock:
        if full:
            return json.loads(json.dumps(_state, default=str))
        return {k: _state.get(k) for k in ("status", "phase", "progress", "current", "started", "finished", "error", "rev")}


def _load_saved() -> None:
    if not RESULT_FILE.exists():
        return
    try:
        data = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    with _lock:
        if _state["status"] != "idle":
            return
        for k in ("roots", "stats", "big", "duplicates", "folder_dups", "junk", "finished", "started"):
            if k in data:
                _state[k] = data[k]
        _state["status"] = "done"
        _state["from_cache"] = True
    _rebuild_known()


def _rebuild_known() -> None:
    paths: set[str] = set()
    with _lock:
        for row in _state["big"] + _state["junk"]:
            paths.add(row["path"].lower())
        for g in _state["duplicates"]:
            for f in g["files"]:
                paths.add(f["path"].lower())
        for g in _state.get("folder_dups") or []:
            for f in g["folders"]:
                paths.add(f["path"].lower())
    _known_paths.clear()
    _known_paths.update(paths)


def _save() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            data = {k: _state[k] for k in ("roots", "stats", "big", "duplicates", "folder_dups", "junk", "finished", "started")}
        tmp = RESULT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RESULT_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- tarama


def _walk(root: Path, out: list[dict], counters: dict[str, int], cancel: threading.Event) -> None:
    stack = [root]
    while stack:
        if cancel.is_set():
            return
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            counters["errors"] += 1
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() in SKIP_DIRS:
                            continue
                        # Junction / reparse noktalarını izleme
                        st = e.stat(follow_symlinks=False)
                        if getattr(st, "st_file_attributes", 0) & _stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                        stack.append(Path(e.path))
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    counters["errors"] += 1
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                counters["files"] += 1
                if attrs & CLOUD_ATTRS:
                    counters["cloud"] += 1
                    continue
                counters["bytes"] += st.st_size
                out.append(
                    {
                        "path": e.path,
                        "name": e.name,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "atime": st.st_atime,
                    }
                )
                if counters["files"] % 2000 == 0:
                    _set(current=d.__str__())


def _root_label(path: str, roots: list[dict]) -> str:
    low = path.lower()
    best = ""
    label = ""
    for r in roots:
        rp = r["path"].lower().rstrip("\\") + "\\"
        if low.startswith(rp) and len(rp) > len(best):
            best = rp
            label = r["label"]
    return label


def _row(f: dict, roots: list[dict], **extra: Any) -> dict[str, Any]:
    p = Path(f["path"])
    return {
        "path": f["path"],
        "name": f["name"],
        "dir": str(p.parent),
        "size": f["size"],
        "mtime": f["mtime"],
        "root": _root_label(f["path"], roots),
        **extra,
    }


def _junk_reasons(f: dict, root_label: str, now: float, sibling_dirs: set[str]) -> list[str]:
    p = Path(f["path"])
    ext = p.suffix.lower()
    stem = p.stem
    reasons = []
    loose = root_label in ("İndirilenler", "Masaüstü")
    age_days = (now - f["mtime"]) / 86400
    if ext in PARTIAL_EXT:
        reasons.append("Yarım kalmış indirme / geçici dosya")
    if loose and ext in INSTALLER_EXT:
        reasons.append("Kurulum dosyası (program kurulduysa gerekmez)")
    if loose and ext in ARCHIVE_EXT:
        if str(p.with_suffix("")).lower() in sibling_dirs or str(p.parent / stem.split(".")[0]).lower() in sibling_dirs:
            reasons.append("Arşiv zaten açılmış (yanında aynı adlı klasör var)")
        elif age_days > ARCHIVE_OLD_DAYS:
            reasons.append(f"{int(age_days)} gündür duran arşiv")
    if root_label == "İndirilenler" and age_days > OLD_DAYS and f["size"] >= OLD_MIN_BYTES and not reasons:
        reasons.append(f"{int(age_days // 30)} aydır değişmemiş indirme")
    if COPY_NAME_RE.search(stem):
        reasons.append("Adı kopya gibi (Kopya / (1))")
    return reasons


def _load_hash_cache() -> dict[str, str]:
    try:
        return json.loads(HASH_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_hash_cache(cache: dict[str, str]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Şişmesin
        if len(cache) > 200_000:
            cache = dict(list(cache.items())[-150_000:])
        HASH_CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def _ck(f: dict) -> str:
    return f"{f['path'].lower()}|{f['size']}|{int(f['mtime'])}"


def _partial_hash(f: dict) -> str | None:
    try:
        with open(f["path"], "rb") as fh:
            h = hashlib.blake2b(digest_size=16)
            h.update(fh.read(PARTIAL_HEAD))
            if f["size"] > PARTIAL_HEAD * 2:
                fh.seek(-PARTIAL_HEAD, os.SEEK_END)
                h.update(fh.read(PARTIAL_HEAD))
            return h.hexdigest()
    except OSError:
        return None


def _full_hash(f: dict, cancel: threading.Event, progress: dict) -> str | None:
    try:
        h = hashlib.blake2b(digest_size=20)
        with open(f["path"], "rb") as fh:
            while True:
                if cancel.is_set():
                    return None
                chunk = fh.read(4 * 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                with _lock:
                    progress["done"] += len(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _keep_score(f: dict, root_label: str) -> tuple:
    """Küçük skor = tutulmaya daha uygun."""
    pref = {"Belgeler": 0, "Resimler": 0, "Videolar": 0, "Müzik": 0, "OneDrive": 1, "YandexDisk": 1, "Masaüstü": 2, "İndirilenler": 3}
    stem = Path(f["path"]).stem
    return (
        1 if COPY_NAME_RE.search(stem) else 0,
        pref.get(root_label, 1),
        f["mtime"],
        len(f["path"]),
    )


def _find_duplicates(
    files: list[dict], roots: list[dict], cancel: threading.Event
) -> tuple[list[dict], dict[str, str]]:
    """Dosya kopyaları + her aday dosyanın içerik imzası (klasör karşılaştırması için)."""
    by_size: dict[int, list[dict]] = defaultdict(list)
    for f in files:
        if f["size"] >= DUP_MIN_BYTES:
            by_size[f["size"]].append(f)

    # Sabit bağlantıları ele (aynı fiziksel dosya). scandir Windows'ta inode
    # vermez (0 döner); gerçek kimlik yalnız adaylar için os.stat ile alınır.
    def ident(f: dict) -> tuple:
        try:
            s = os.stat(f["path"])
            if s.st_ino:
                return (s.st_dev, s.st_ino)
        except OSError:
            pass
        return ("path", f["path"].lower())

    cands: list[list[dict]] = []
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        uniq = {}
        for f in group:
            uniq.setdefault(ident(f), f)
        if len(uniq) > 1:
            cands.append(list(uniq.values()))

    _set(phase="Kopya adayları (hızlı karşılaştırma)", progress=0)
    flat = [f for g in cands for f in g]
    with ThreadPoolExecutor(max_workers=6) as ex:
        partials = list(ex.map(_partial_hash, flat))
    pmap = {id(f): h for f, h in zip(flat, partials)}

    stage2: list[list[dict]] = []
    for g in cands:
        sub: dict[str, list[dict]] = defaultdict(list)
        for f in g:
            h = pmap.get(id(f))
            if h:
                sub[h].append(f)
        stage2.extend(v for v in sub.values() if len(v) > 1)

    cache = _load_hash_cache()
    to_hash = [f for g in stage2 for f in g if _ck(f) not in cache]
    progress = {"done": 0, "total": sum(f["size"] for f in to_hash) or 1}
    _set(phase=f"Tam içerik karşılaştırma ({len(to_hash)} dosya)", progress=0)

    def work(f: dict) -> None:
        if cancel.is_set():
            return
        _set(current=f["path"])
        h = _full_hash(f, cancel, progress)
        if h:
            with _lock:
                cache[_ck(f)] = h
                _state["progress"] = min(99, int(progress["done"] / progress["total"] * 100))

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(work, to_hash))
    if cancel.is_set():
        return [], {}
    _save_hash_cache(cache)

    sigs: dict[str, str] = {}
    for f in flat:
        h = cache.get(_ck(f))
        if h:
            sigs[f["path"].lower()] = h
        elif pmap.get(id(f)):
            sigs[f["path"].lower()] = "p:" + pmap[id(f)]

    groups: list[dict] = []
    for g in stage2:
        sub: dict[str, list[dict]] = defaultdict(list)
        for f in g:
            h = cache.get(_ck(f))
            if h:
                sub[h].append(f)
        for h, same in sub.items():
            if len(same) < 2:
                continue
            labeled = [(f, _root_label(f["path"], roots)) for f in same]
            labeled.sort(key=lambda t: _keep_score(t[0], t[1]))
            rows = []
            for i, (f, lab) in enumerate(labeled):
                rows.append(_row(f, roots, keep=(i == 0)))
            names = {r["name"].lower() for r in rows}
            groups.append(
                {
                    "hash": h,
                    "size": same[0]["size"],
                    "count": len(rows),
                    "wasted": same[0]["size"] * (len(rows) - 1),
                    "different_names": len(names) > 1,
                    "files": rows,
                }
            )
    groups.sort(key=lambda g: g["wasted"], reverse=True)
    return groups, sigs


FOLDER_MIN_BYTES = 20 * 1024 * 1024
BACKUPISH_RE = re.compile(r"yedek|backup|kopya|copy|eski|old|bak\b|\(\d+\)", re.IGNORECASE)


def _find_folder_duplicates(
    files: list[dict], roots: list[str], sigs: dict[str, str], cfg_roots: list[dict]
) -> list[dict]:
    """İçeriği birebir aynı klasörler (Merkle ağacı: ad + boyut + içerik imzası).

    Küçük dosyalar (< DUP_MIN_BYTES) ad+boyutla, büyükler içerik hash'iyle karşılaştırılır.
    Atlanan klasörler (node_modules, .git…) karşılaştırmaya girmez.
    """
    root_set = {r.lower().rstrip("\\") for r in roots}
    children: dict[str, list[tuple]] = defaultdict(list)
    subdirs: dict[str, set[str]] = defaultdict(set)
    real_name: dict[str, str] = {}
    for f in files:
        par = os.path.dirname(f["path"])
        key = par.lower()
        real_name.setdefault(key, par)
        sig = sigs.get(f["path"].lower(), "")
        children[key].append((f["name"].lower(), f"{f['size']}:{sig}", f["size"], 1))
        cur, cur_real = key, par
        while cur not in root_set:
            parent, parent_real = os.path.dirname(cur), os.path.dirname(cur_real)
            if parent == cur:
                break
            real_name.setdefault(parent, parent_real)
            if cur in subdirs[parent]:
                break
            subdirs[parent].add(cur)
            cur, cur_real = parent, parent_real

    memo: dict[str, tuple[str, int, int]] = {}

    def node(d: str) -> None:
        stack = [(d, False)]
        while stack:
            cur, done = stack.pop()
            if cur in memo:
                continue
            if not done:
                stack.append((cur, True))
                for sd in subdirs.get(cur, ()):
                    if sd not in memo:
                        stack.append((sd, False))
                continue
            entries = list(children.get(cur, ()))
            for sd in subdirs.get(cur, ()):
                h, b, n = memo[sd]
                entries.append((os.path.basename(sd), "d:" + h, b, n))
            entries.sort()
            hh = hashlib.blake2b(digest_size=16)
            total = count = 0
            for name, sig, b, n in entries:
                hh.update(f"{name}\0{sig}\0".encode("utf-8", "surrogatepass"))
                total += b
                count += n
            memo[cur] = (hh.hexdigest(), total, count)

    for d in list(subdirs.keys()) + list(children.keys()):
        node(d)

    by_sig: dict[str, list[str]] = defaultdict(list)
    for d, (h, b, n) in memo.items():
        if d in root_set or b < FOLDER_MIN_BYTES or n < 2:
            continue
        by_sig[h].append(d)
    dup_sigs = {h for h, ds in by_sig.items() if len(ds) > 1}

    groups = []
    for h in dup_sigs:
        ds = by_sig[h]
        # Üst klasörleri de birbirinin kopyasıysa bu grup orada zaten görünür
        if all(memo.get(os.path.dirname(d), ("",))[0] in dup_sigs for d in ds):
            continue
        _, b, n = memo[ds[0]]
        rows = []
        for d in ds:
            path = real_name.get(d, d)
            rows.append(
                {
                    "path": path,
                    "name": os.path.basename(path),
                    "dir": os.path.dirname(path),
                    "size": b,
                    "files": n,
                    "root": _root_label(path, cfg_roots),
                    "is_dir": True,
                }
            )
        rows.sort(key=lambda r: (1 if BACKUPISH_RE.search(r["path"]) else 0, r["path"].count("\\"), len(r["path"])))
        for i, r in enumerate(rows):
            r["keep"] = i == 0
        groups.append(
            {"hash": h, "size": b, "files": n, "count": len(rows), "wasted": b * (len(rows) - 1), "folders": rows}
        )
    groups.sort(key=lambda g: g["wasted"], reverse=True)
    return groups


_cancel = threading.Event()


def _run(roots: list[dict]) -> None:
    _cancel.clear()
    _set(
        status="running",
        phase="Dosyalar listeleniyor",
        progress=0,
        current="",
        started=time.time(),
        finished=None,
        error=None,
        roots=roots,
        from_cache=False,
    )
    try:
        files: list[dict] = []
        counters = {"files": 0, "bytes": 0, "cloud": 0, "errors": 0}
        # İç içe kökleri tekle (ör. Belgeler içindeki bir ek klasör)
        paths = sorted({str(Path(r["path"])) for r in roots if Path(r["path"]).exists()}, key=len)
        uniq_roots: list[str] = []
        for p in paths:
            if not any(p.lower().startswith(q.lower().rstrip("\\") + "\\") for q in uniq_roots):
                uniq_roots.append(p)
        for i, p in enumerate(uniq_roots):
            _set(current=p, progress=int(i / max(len(uniq_roots), 1) * 100))
            _walk(Path(p), files, counters, _cancel)
            if _cancel.is_set():
                _set(status="cancelled", finished=time.time(), phase="İptal edildi")
                return

        now = time.time()
        _set(phase="Büyük ve gereksiz dosyalar", progress=0)

        big = sorted((f for f in files if f["size"] >= BIG_BYTES), key=lambda f: f["size"], reverse=True)
        big_rows = [_row(f, roots) for f in big[:MAX_LIST]]

        # Arşiv açılmış mı kontrolü için klasör adları
        parents = {str(Path(f["path"]).parent).lower() for f in files}
        sibling_dirs: set[str] = set()
        for par in parents:
            try:
                for e in os.scandir(par):
                    if e.is_dir(follow_symlinks=False):
                        sibling_dirs.add(e.path.lower())
            except OSError:
                pass

        junk_rows = []
        for f in files:
            lab = _root_label(f["path"], roots)
            reasons = _junk_reasons(f, lab, now, sibling_dirs)
            if reasons and (f["size"] >= 1024 * 1024 or Path(f["path"]).suffix.lower() in PARTIAL_EXT):
                junk_rows.append(_row(f, roots, reasons=reasons))
        junk_rows.sort(key=lambda r: r["size"], reverse=True)
        junk_rows = junk_rows[:MAX_LIST]

        dups, sigs = _find_duplicates(files, roots, _cancel)
        if _cancel.is_set():
            _set(status="cancelled", finished=time.time(), phase="İptal edildi")
            return

        _set(phase="Kopya klasörler", progress=99)
        fdups = _find_folder_duplicates(files, uniq_roots, sigs, roots)
        # Kopya klasörlerin içindeki dosya kopyaları ayrıca listelenmez
        covered = tuple(r["path"].lower().rstrip("\\") + "\\" for g in fdups for r in g["folders"])
        file_dups = []
        for g in dups:
            inside = sum(1 for f in g["files"] if f["path"].lower().startswith(covered))
            if inside == len(g["files"]):
                continue
            if inside:
                # Klasör kopyalarındaki fazlalık orada sayıldı; burada yalnız dışarıdakiler
                outside = len(g["files"]) - inside
                g = {**g, "wasted": g["size"] * outside, "in_dup_folder": inside}
            file_dups.append(g)
        file_dups.sort(key=lambda g: g["wasted"], reverse=True)

        stats = {
            "files": counters["files"],
            "bytes": counters["bytes"],
            "cloud_skipped": counters["cloud"],
            "errors": counters["errors"],
            "big_count": len(big),
            "big_bytes": sum(f["size"] for f in big),
            "dup_groups": len(file_dups),
            "dup_wasted": sum(g["wasted"] for g in file_dups),
            "folder_dup_groups": len(fdups),
            "folder_dup_wasted": sum(g["wasted"] for g in fdups),
            "junk_count": len(junk_rows),
            "junk_bytes": sum(r["size"] for r in junk_rows),
            "seconds": round(time.time() - (_state.get("started") or now), 1),
        }
        _set(
            status="done",
            phase="Tamam",
            progress=100,
            current="",
            finished=time.time(),
            stats=stats,
            big=big_rows,
            duplicates=file_dups[:MAX_LIST],
            folder_dups=fdups[:MAX_LIST],
            junk=junk_rows,
            trashed=[],
            rev=(_state.get("rev") or 0) + 1,
        )
        _rebuild_known()
        _save()
    except Exception as e:  # pragma: no cover
        _set(status="error", error=str(e), finished=time.time())


def start_files_scan(roots: list[dict] | None = None, background: bool = True) -> dict[str, Any]:
    with _lock:
        if _state["status"] == "running":
            return {"ok": False, "error": "Tarama zaten sürüyor"}
    roots = roots or [r for r in default_roots() if r["default"] and r["exists"]]
    clean: list[dict] = []
    for r in roots:
        p = str(r.get("path") or "").strip().strip('"')
        if not p:
            continue
        pp = Path(p)
        if not pp.is_absolute() or not pp.exists():
            continue
        clean.append({"label": r.get("label") or pp.name, "path": str(pp)})
    if not clean:
        return {"ok": False, "error": "Taranacak klasör yok"}
    if background:
        threading.Thread(target=_run, args=(clean,), daemon=True, name="files-scan").start()
    else:
        _run(clean)
    return {"ok": True}


def cancel_files_scan() -> dict[str, Any]:
    _cancel.set()
    return {"ok": True}


# ---------------------------------------------------------------- işlemler


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _recycle(path: str) -> bool:
    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0"  # çift NUL ile biter (ctypes bir tane daha ekler)
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)


def _recycle_limits(path: str) -> tuple[bool, int | None]:
    """(kalıcı_silme_açık, kutu_kapasitesi_bayt) — bilinmiyorsa (False, None)."""
    try:
        import winreg

        drive = os.path.splitdrive(path)[0] + "\\"
        buf = ctypes.create_unicode_buffer(100)
        if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, buf, 100):
            return False, None
        guid = buf.value.split("Volume", 1)[1].strip("\\")  # {....}
        key = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            def val(name: str) -> int | None:
                try:
                    return int(winreg.QueryValueEx(k, name)[0])
                except OSError:
                    return None

            nuke = val("NukeOnDelete") == 1
            cap = val("MaxCapacity")
            return nuke, (cap * 1024 * 1024 if cap else None)
    except Exception:
        return False, None


def _item_sizes() -> dict[str, int]:
    sizes: dict[str, int] = {}
    with _lock:
        for r in _state["big"] + _state["junk"]:
            sizes[r["path"].lower()] = r["size"]
        for g in _state["duplicates"]:
            for f in g["files"]:
                sizes[f["path"].lower()] = f["size"]
        for g in _state.get("folder_dups") or []:
            for f in g["folders"]:
                sizes[f["path"].lower()] = f["size"]
    return sizes


def _under(low: str, folders: tuple[str, ...]) -> bool:
    return low.startswith(folders)


def plan_trash(paths: list[str]) -> tuple[list[dict], list[dict]]:
    """Seçimi doğrular: (taşınacaklar, reddedilenler).

    - yalnız son taramada listelenen ve hâlâ var olan yollar,
    - seçili bir klasörün içindeki seçimler tekilleştirilir (klasörle birlikte gider),
    - bir kopya grubunun bütün üyelerini götürecek seçim reddedilir (son kopya korunur),
    - Geri Dönüşüm Kutusu kapalıysa ya da öğe kutuya sığmıyorsa reddedilir (kalıcı silinirdi).
    """
    sizes = _item_sizes()
    rejected: list[dict] = []
    chosen: dict[str, str] = {}
    for p in paths:
        p = str(p)
        low = p.lower()
        if low not in _known_paths:
            rejected.append({"path": p, "error": "Son taramada yok"})
        elif not os.path.exists(p):
            rejected.append({"path": p, "error": "Bulunamadı (zaten taşınmış olabilir)"})
        else:
            chosen[low] = p

    # Son kopya koruması: grup üyelerinin hepsi gidiyorsa "tut" üyesini kurtar
    with _lock:
        groups = [[f for f in g["folders"]] for g in _state.get("folder_dups") or []]
        groups += [[f for f in g["files"]] for g in _state["duplicates"]]
    changed = True
    while changed:
        changed = False
        sel_dirs = tuple(c.rstrip("\\") + "\\" for c in chosen)
        for members in groups:
            lows = [m["path"].lower() for m in members]
            if not all(lw in chosen or _under(lw, sel_dirs) for lw in lows):
                continue
            keep = next((m for m in members if m.get("keep")), members[0])["path"].lower()
            # tut üyesini götüren seçimleri (kendisi ya da üst klasörü) bırak
            for c in list(chosen):
                if keep == c or keep.startswith(c.rstrip("\\") + "\\"):
                    rejected.append({"path": chosen.pop(c), "error": "Grubun son kopyası; en az biri kalmalı"})
                    changed = True
            if changed:
                break

    # İç içe seçimleri tekle
    order = sorted(chosen, key=len)
    tops: list[str] = []
    for c in order:
        if not any(c.startswith(t.rstrip("\\") + "\\") for t in tops):
            tops.append(c)

    nuke, cap = _recycle_limits(next(iter(chosen.values()), "C:\\"))
    items: list[dict] = []
    for c in tops:
        p = chosen[c]
        size = sizes.get(c, 0)
        if nuke:
            rejected.append({"path": p, "error": "Geri Dönüşüm Kutusu kapalı (kalıcı silinirdi)"})
        elif cap and size > cap * 0.9:
            rejected.append({"path": p, "error": "Geri Dönüşüm Kutusu'na sığmaz (kalıcı silinirdi)"})
        else:
            items.append({"path": p, "size": size})
    return items, rejected


def _apply_trashed(done: list[str]) -> None:
    if not done:
        return
    gone_set = {d.lower() for d in done}
    gone_pref = tuple(d.rstrip("\\") + "\\" for d in gone_set)

    def gone(low: str) -> bool:
        return low in gone_set or low.startswith(gone_pref)

    with _lock:
        _state["big"] = [r for r in _state["big"] if not gone(r["path"].lower())]
        _state["junk"] = [r for r in _state["junk"] if not gone(r["path"].lower())]
        groups = []
        for g in _state["duplicates"]:
            files = [f for f in g["files"] if not gone(f["path"].lower())]
            if len(files) >= 2:
                groups.append({**g, "files": files, "count": len(files), "wasted": g["size"] * (len(files) - 1)})
        _state["duplicates"] = groups
        fgroups = []
        for g in _state.get("folder_dups") or []:
            # Üyelerinden birinin içinden bir şey taşındıysa içerik değişti: grup artık geçersiz
            inner = tuple(f["path"].lower().rstrip("\\") + "\\" for f in g["folders"])
            if any(d.startswith(inner) for d in gone_set):
                continue
            rows = [f for f in g["folders"] if not gone(f["path"].lower())]
            if len(rows) >= 2:
                fgroups.append({**g, "folders": rows, "count": len(rows), "wasted": g["size"] * (len(rows) - 1)})
        _state["folder_dups"] = fgroups
        _state["trashed"] = (_state.get("trashed") or []) + done
        _state["rev"] = (_state.get("rev") or 0) + 1
    _save()


def _fmt_short(n: float) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    return f"{n / 1024**2:.0f} MB"


def enqueue_trash(paths: list[str]) -> dict[str, Any]:
    """Seçimi doğrular ve taşımayı arka plan işi olarak sıraya alır."""
    from job_queue import enqueue_task, update_job

    items, rejected = plan_trash(paths)
    if not items:
        return {"ok": False, "error": "Taşınacak öğe kalmadı", "rejected": rejected}
    total_bytes = sum(i["size"] for i in items)

    def work(job: dict) -> dict[str, Any]:
        done: list[str] = []
        failed: list[dict] = []
        moved = 0
        batch: list[str] = []
        for n, it in enumerate(items, 1):
            if job.get("cancel_requested"):
                break
            name = os.path.basename(it["path"].rstrip("\\"))
            update_job(
                job,
                progress=int((n - 1) / len(items) * 100),
                detail=f"{n}/{len(items)} · {_fmt_short(moved)} / {_fmt_short(total_bytes)} · {name}",
            )
            if not os.path.exists(it["path"]):
                failed.append({"path": it["path"], "error": "Bulunamadı"})
                continue
            if _recycle(it["path"]):
                done.append(it["path"])
                batch.append(it["path"])
                moved += it["size"]
            else:
                failed.append({"path": it["path"], "error": "Taşınamadı (açık ya da izin yok)"})
            # Listeler taşıdıkça güncellensin (her 5 öğede bir)
            if len(batch) >= 5:
                _apply_trashed(batch)
                batch = []
        _apply_trashed(batch)
        cancelled = bool(job.get("cancel_requested"))
        summary = f"{len(done)} öğe · ~{_fmt_short(moved)} Geri Dönüşüm Kutusu'na taşındı"
        if failed:
            summary += f" · {len(failed)} taşınamadı"
        if cancelled:
            summary += " · durduruldu"
        update_job(job, progress=100, detail=summary)
        log_paths(done, failed)
        return {
            "ok": not failed,
            "cancelled": cancelled,
            "note": summary,
            "error": "; ".join(f"{os.path.basename(f['path'])}: {f['error']}" for f in failed[:5]) or None,
            "trashed": done,
            "failed": failed,
        }

    title = f"Geri Dönüşüm Kutusu'na taşı · {len(items)} öğe · {_fmt_short(total_bytes)}"
    res = enqueue_task("files_trash", title, work)
    return {"ok": True, "job": res["job"], "count": len(items), "bytes": total_bytes, "rejected": rejected}


def log_paths(done: list[str], failed: list[dict]) -> None:
    try:
        from applog import log

        log("files_trash", count=len(done), failed=len(failed), paths=done, errors=failed[:20])
    except Exception:
        pass


def reveal_path(path: str) -> dict[str, Any]:
    if str(path).lower() not in _known_paths or not os.path.exists(path):
        return {"ok": False, "error": "Taramada yok"}
    subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])
    return {"ok": True}


_load_saved()


# ---------------------------------------------------------------- komut satırı


def _fmt(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Büyük / gereksiz / kopya dosya taraması")
    ap.add_argument("paths", nargs="*", help="Taranacak klasörler (boşsa kullanıcı klasörleri)")
    ap.add_argument("--json", help="Sonucu bu dosyaya yaz")
    args = ap.parse_args()
    roots = [{"label": Path(p).name, "path": p} for p in args.paths] or None
    res = start_files_scan(roots, background=False)
    if not res.get("ok"):
        raise SystemExit(res.get("error"))
    st = get_files_state()
    s = st["stats"]
    print(f"{s['files']} dosya, {_fmt(s['bytes'])} · bulutta atlanan {s['cloud_skipped']} · {s['seconds']} sn")
    print(f"Büyük: {s['big_count']} dosya, {_fmt(s['big_bytes'])}")
    print(f"Kopya klasör: {s['folder_dup_groups']} grup, boşa {_fmt(s['folder_dup_wasted'])}")
    print(f"Kopya dosya: {s['dup_groups']} grup, boşa {_fmt(s['dup_wasted'])}")
    print(f"Gereksiz olabilir: {s['junk_count']} dosya, {_fmt(s['junk_bytes'])}")
    if args.json:
        Path(args.json).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
