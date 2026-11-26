#!/usr/bin/env python3
"""Sonar web UI: a modern local front end for search, sort, expiring and backup.

Stdlib only (http.server). Binds to 127.0.0.1. Every action that opens a file,
moves files, copies files or rebuilds an index needs POST plus the per-launch
X-Sonar-Token header. Nothing is ever deleted or overwritten: Sort uses the
no-overwrite mover and Backup runs the dry-run-first sync script.

    python scripts/sonar_web.py                 # open in your browser
    python scripts/sonar_web.py --port 8799 --no-browser
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_PROJECT = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# The same UI runs on two module layouts: family_* (personal project) and
# sonar* (public kit). Pick whichever is next to this file.
try:
    import family_search as S  # type: ignore
    import family_sort as SO  # type: ignore
    BACKUP_SCRIPT = _SCRIPTS / "sync_to_m.py"
    BACKUP_MODULE = "sync_to_m"
except ImportError:  # public kit layout
    import sonar as S  # type: ignore
    import sonar_sort as SO  # type: ignore
    BACKUP_SCRIPT = _SCRIPTS / "sonar_backup.py"
    BACKUP_MODULE = "sonar_backup"

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = None

UI_FILE = _SCRIPTS / "sonar_web_ui.html"
PLANS_DIR = _PROJECT / "plans"
DEFAULT_PORT = 8765
SEARCH_LIMIT = 100  # fast first page (FTS-ranked); "Show more" asks for MORE_LIMIT
MORE_LIMIT = 1000
BROWSE_LIMIT = 5000
EBOOKS_TYPE = "eBooks"
FILE_TYPES = ["PDF", "Image", "Word", "Excel", "Text", "Video", "eBooks", "Other"]
WIDEN_STEPS = (30, 90, 180, 365, 730)
IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log", ".ini", ".py", ".ps1", ".xml", ".html", ".yml", ".yaml"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024

TOKEN = secrets.token_urlsafe(24)
_LOCK = threading.RLock()
_KNOWN_PATHS: set[str] = set()
_SORT_PLANS: dict[str, list] = {}
_JOBS: dict[str, dict] = {}
_BACKUP_STATE: dict = {"dry_run": None, "live_log": ""}
_CHILDREN: set = set()
_CACHE: dict = {}
CACHE_TTL = 300


def _cached(key: tuple, fn):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    value = fn()
    with _LOCK:
        _CACHE[key] = (now, value)
    return value


def _clear_caches() -> None:
    with _LOCK:
        _CACHE.clear()


# ----------------------------------------------------------------- helpers

def log(*parts: object) -> None:
    if sys.stdout is not None:
        try:
            print(*parts, flush=True)
        except Exception:
            pass


def now_text() -> str:
    when = datetime.now(ET) if ET else datetime.now()
    return when.strftime("%Y-%m-%d %I:%M %p") + (" ET" if ET else "")


def fmt_mtime(ns: int) -> str:
    if not ns:
        return ""
    try:
        when = datetime.fromtimestamp(ns / 1e9, ET) if ET else datetime.fromtimestamp(ns / 1e9)
        return when.strftime("%Y-%m-%d %I:%M %p")
    except (OSError, ValueError, OverflowError):
        return ""


def remember(path: Path | str) -> str:
    text = str(path)
    with _LOCK:
        _KNOWN_PATHS.add(os.path.normcase(text))
    return text


def is_known(path: str) -> bool:
    with _LOCK:
        return os.path.normcase(path) in _KNOWN_PATHS


def load_cfg() -> dict:
    return S.load_config(None)


def load_rules_safe() -> dict:
    try:
        return SO.load_rules()
    except Exception:
        return {}


def cabinet_root() -> Path | None:
    rules = load_rules_safe()
    fn = getattr(SO, "family_root", None) or getattr(SO, "cabinet_root", None)
    try:
        return Path(fn(rules, None)) if fn else None
    except Exception:
        return None


def top_folders(cfg: dict) -> list[str]:
    try:
        from sonar_index import load_settings  # type: ignore

        folders = load_settings().get("folders")
        if folders:
            return list(folders)
    except Exception:
        pass
    for root in cfg.get("roots") or []:
        if root.get("include"):
            return list(root["include"])
    base = cabinet_root()
    if base and base.is_dir():
        try:
            return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name[:2].isdigit())
        except OSError:
            pass
    return []


def people() -> list[str]:
    return list((load_rules_safe().get("people") or {}).keys())


_DRIVE_CACHE: dict[str, tuple[float, str | None]] = {}


def offline_message(path: Path | str) -> str | None:
    """disconnected_drive_message, cached per drive letter for a few seconds."""
    drive = Path(path).drive.upper()
    if not drive:
        return None
    hit = _DRIVE_CACHE.get(drive)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    msg = S.disconnected_drive_message(path)
    _DRIVE_CACHE[drive] = (time.time(), msg)
    return msg


def rel_to_cabinet(dest: Path | None) -> str:
    if dest is None or not str(dest) or str(dest) == ".":
        return ""
    root = cabinet_root()
    for base in ([root, root.parent] if root is not None else []):
        try:
            return str(dest.relative_to(base))
        except ValueError:
            continue
    return str(dest)


TYPE_BY_EXT = {}
for _label, _exts in {
    "PDF": ".pdf",
    "Image": ".png .jpg .jpeg .webp .gif .bmp .tif .tiff .heic .heif .svg",
    "Word": ".doc .docx .odt .rtf .pages",
    "Excel": ".xls .xlsx .xlsm .csv .ods .numbers",
    "eBook": ".epub .mobi .azw .azw3 .kfx .djvu .fb2 .prc",
    "Text": ".txt .md .log .json .xml .html .htm .ini .yml .yaml",
    "Video": ".mp4 .mov .avi .mkv .wmv .m4v .webm",
    "Slides": ".ppt .pptx .key .odp",
    "Audio": ".mp3 .m4a .wav .flac .aac",
    "Archive": ".zip .rar .7z .tar .gz",
}.items():
    for _e in _exts.split():
        TYPE_BY_EXT[_e] = _label


def file_type(path: Path) -> str:
    return TYPE_BY_EXT.get(path.suffix.lower(), "Other")


def hit_json(hit) -> dict:
    full = Path(hit.full_path)
    offline = offline_message(full)
    return {
        "name": hit.name,
        "drive": hit.source or "Local",
        "online": offline is None,
        "offline": offline or "",
        "folder": hit.folder,
        "dir": remember(full.parent),
        "path": remember(full),
        "modified": fmt_mtime(hit.mtime_ns),
        "mtime": hit.mtime_ns,
        "size": hit.size,
        "snippet": hit.snippet or "",
        "ext": full.suffix.lower(),
        "type": file_type(full),
    }


def source_status_list() -> list[dict]:
    _DRIVE_CACHE.clear()
    cfg = load_cfg()
    stamps: dict[str, float] = {}
    roots_by: dict[str, list[str]] = {}
    for src in S.configured_sources(cfg):
        try:
            db = Path(S.cfg_for_source(src, cfg)["index_db"])
        except Exception:
            db = Path(src.db)
        mtimes = []
        for cand in (db, db.with_name(db.name + "-wal")):
            try:
                mtimes.append(cand.stat().st_mtime)
            except OSError:
                pass
        if mtimes:
            stamps[src.name] = max(mtimes)
        try:
            roots_by[src.name] = [str(r.get("path")) for r in (S.cfg_for_source(src, cfg).get("roots") or []) if r.get("path")]
        except Exception:
            roots_by[src.name] = []
    out = []
    for st in S.source_statuses(cfg):
        ts = stamps.get(st.name)
        short = ""
        if ts:
            when = datetime.fromtimestamp(ts, ET) if ET else datetime.fromtimestamp(ts)
            short = when.strftime("%b %d, %I:%M %p").replace(" 0", " ")
        out.append({"name": st.name, "label": st.label, "state": st.state,
                    "message": st.message, "indexed_at": st.indexed_at,
                    "indexed_ts": ts or 0, "indexed_short": short,
                    "stale": bool(ts and time.time() - ts > 86400),
                    "roots": roots_by.get(st.name, [])})
    return out


# ------------------------------------------------------------------- jobs

def start_job(kind: str, fn) -> dict:
    with _LOCK:
        for job in _JOBS.values():
            if job["kind"] == kind and job["state"] == "running":
                return job
        job_id = secrets.token_hex(6)
        job = {"id": job_id, "kind": kind, "state": "running", "started": time.time(),
               "result": None, "error": ""}
        _JOBS[job_id] = job

    def run() -> None:
        try:
            job["result"] = fn()
            job["state"] = "done"
        except Exception as exc:  # report, never crash the server
            job["error"] = f"{exc}"
            job["state"] = "error"
            log(traceback.format_exc())
        job["elapsed"] = round(time.time() - job["started"], 1)

    threading.Thread(target=run, daemon=True).start()
    return job


def job_view(job: dict) -> dict:
    view = {k: job.get(k) for k in ("id", "kind", "state", "result", "error", "elapsed")}
    if job["state"] == "running":
        view["elapsed"] = round(time.time() - job["started"], 1)
        live = _BACKUP_STATE.get("live_log") if job["kind"].startswith("backup") else ""
        if live:
            try:
                with open(live, "rb") as fh:
                    view["progress"] = max(0, fh.read().count(b"\n") - 1)
            except OSError:
                pass
    return view


# ------------------------------------------------------------------ search

def show_ebooks_shortcut(cfg: dict) -> bool:
    """Config `show_ebooks_shortcut` wins; otherwise show it only if the index has eBooks."""
    setting = cfg.get("show_ebooks_shortcut")
    try:
        from sonar_index import load_settings  # type: ignore

        setting = load_settings().get("show_ebooks_shortcut", setting)
    except Exception:
        pass
    if isinstance(setting, bool):
        return setting
    try:
        return bool(S.browse(limit=1, file_type=EBOOKS_TYPE, source="all", warnings=[]))
    except Exception:
        return False


_META_CACHE: dict = {}


def api_meta() -> dict:
    with _LOCK:
        cached = _META_CACHE.get("meta")
    if cached:
        return {**cached, "backup": backup_target_info(), "now": now_text()}
    meta = _build_meta()
    with _LOCK:
        _META_CACHE["meta"] = meta
    return meta


def prewarm() -> None:
    """Build the settings cache and pull index pages into the OS cache, off the request path."""
    try:
        meta = api_meta()
        S.search("a", limit=5, source="all", warnings=[])
        if meta.get("show_ebooks"):
            api_search({"type": EBOOKS_TYPE, "source": "all"})
        api_expiring({"days": "180", "source": "all"})
    except Exception:
        pass


def _build_meta() -> dict:
    cfg = load_cfg()
    sources = [{"name": s.name, "label": s.label} for s in S.configured_sources(cfg)]
    base = cabinet_root()
    inbox = next((f for f in top_folders(cfg) if "inbox" in f.lower()), "00 Inbox")
    presets = []
    if base:
        presets.append({"label": inbox, "path": str(base / inbox)})
    presets.append({"label": "Downloads", "path": str(Path.home() / "Downloads")})
    presets.append({"label": "Desktop", "path": str(Path.home() / "Desktop")})
    return {
        "sources": sources,
        "folders": top_folders(cfg),
        "people": people(),
        "types": FILE_TYPES,
        "presets": presets,
        "backup": backup_target_info(),
        "show_ebooks": show_ebooks_shortcut(cfg),
        "now": now_text(),
    }


def api_search(qs: dict) -> dict:
    q = (qs.get("q") or "").strip()
    folder = qs.get("folder") or None
    person = qs.get("person") or None
    ftype = qs.get("type") or None
    date_from = qs.get("from") or None
    date_to = qs.get("to") or None
    source = qs.get("source") or "all"
    warnings: list[str] = []
    t0 = time.perf_counter()
    limit = 0
    if not q:
        if ftype != EBOOKS_TYPE:
            return {"hits": [], "mode": "idle", "warnings": [], "ms": 0,
                    "message": "Type a search and press Enter, or open the eBooks library."}
        hits, cached_warn = _cached(("browse", folder, person, ftype, source), lambda: (
            lambda w: (S.browse(limit=BROWSE_LIMIT, folder=folder, person=person, file_type=ftype,
                                source=source, warnings=w), w))([]))
        warnings.extend(cached_warn)
        mode = "browse"
    else:
        limit = max(1, min(int(qs.get("limit") or SEARCH_LIMIT), MORE_LIMIT))
        hits, mode = S.search(q, limit=limit, folder=folder, person=person, file_type=ftype,
                              date_from=date_from, date_to=date_to, source=source,
                              warnings=warnings)
    rows = [hit_json(h) for h in hits]
    more = False
    if q:
        per: dict[str, int] = {}
        for h in hits:
            per[h.source] = per.get(h.source, 0) + 1
        more = limit < MORE_LIMIT and any(n >= limit for n in per.values())
    return {"hits": rows, "mode": mode, "warnings": warnings, "more": more,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def api_expiring(qs: dict) -> dict:
    raw = (qs.get("days") or "180").strip()
    window = int(raw) if raw else None
    source = qs.get("source") or "all"
    warnings: list[str] = []
    items, cached_warn = _cached(("expiring", source), lambda: (
        lambda w: (S.expiring(days=None, source=source, warnings=w), w))([]))
    warnings.extend(cached_warn)
    items = sorted(items, key=lambda i: (i.days_left, i.name.lower()))
    shown, later = [], []
    for it in items:
        (later if window is not None and it.days_left > window else shown).append(it)
    rows = []
    for it in shown:
        full = Path(it.full_path)
        offline = offline_message(full)
        label = "past due" if it.days_left < 0 else ("soon" if window is None or it.days_left <= window else "")
        rows.append({
            "name": it.name, "person": it.person or "", "expiry": it.expiry.isoformat(),
            "days_left": it.days_left, "label": label, "folder": it.folder,
            "dir": remember(full.parent), "path": remember(full), "drive": it.source or "Local",
            "online": offline is None, "offline": offline or "", "ext": full.suffix.lower(),
            "type": file_type(full),
        })
    next_window = window
    if later and window is not None:
        nearest = min(i.days_left for i in later)
        next_window = next((s for s in WIDEN_STEPS if s > window and s >= nearest),
                           max(i.days_left for i in later))
    return {
        "items": rows,
        "past_due": sum(1 for r in rows if r["days_left"] < 0),
        "soon": sum(1 for r in rows if r["days_left"] >= 0),
        "later": len(later),
        "next_item": (later[0].name if later else ""),
        "next_days": (later[0].days_left if later else None),
        "next_window": next_window,
        "window": window,
        "warnings": warnings,
    }


# -------------------------------------------------------------------- sort

def api_sort_preview(body: dict) -> dict:
    folder = Path(str(body.get("folder") or "").strip().strip('"')).expanduser()
    if not str(folder) or not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    items, plan = SO.preview_folder(folder, recursive=bool(body.get("recursive")))
    with _LOCK:
        _SORT_PLANS[str(plan)] = items
    remember(plan)
    rows = []
    for i, it in enumerate(items):
        includable = it.action in {"move", "inbox"}
        rows.append({
            "id": i, "name": Path(it.source).name, "source": remember(it.source),
            "dest": remember(it.dest) if includable else "",
            "dest_rel": rel_to_cabinet(Path(it.dest)) if includable else "",
            "rule": it.rule, "confidence": round(float(it.confidence), 2),
            "action": it.action, "note": getattr(it, "note", "") or "",
            "ext": Path(it.source).suffix.lower(),
        })
    counts = SO.action_counts(items) if hasattr(SO, "action_counts") else {}
    return {"plan": str(plan), "plan_name": plan.name, "items": rows, "counts": counts,
            "folder": str(folder)}


def api_sort_apply(body: dict) -> dict:
    plan = str(body.get("plan") or "")
    ids = {int(i) for i in body.get("ids") or []}
    with _LOCK:
        items = _SORT_PLANS.get(plan)
    if items is None:
        raise ValueError("That plan is no longer loaded. Scan & Preview again.")
    chosen = [it for i, it in enumerate(items) if i in ids and it.action in {"move", "inbox"}]
    if not chosen:
        raise ValueError("Nothing selected to apply.")
    plan_path = Path(plan)
    selected = plan_path.with_name(plan_path.stem + "-selected.csv")
    SO.write_plan(chosen, selected)
    result = SO.apply_plan(selected, chosen)
    with _LOCK:
        _SORT_PLANS.pop(plan, None)
    undo = str(result.undo_path) if result.undo_path else ""
    if undo:
        remember(undo)
    return {"moved": result.moved, "skipped": result.skipped, "errors": result.errors,
            "undo": undo, "lines": result.lines[-200:]}


def api_sort_undo(_body: dict) -> dict:
    latest = SO.latest_undo_log()
    if latest is None:
        raise ValueError("No sort undo log found.")
    result = SO.undo_moves(latest)
    done = latest.with_name(latest.stem + "-undone" + latest.suffix)
    try:
        if not done.exists():
            latest.rename(done)  # so the next Undo does not replay this log
    except OSError:
        pass
    return {"moved": result.moved, "skipped": result.skipped, "errors": result.errors,
            "undo_log": str(latest), "lines": result.lines[-200:]}


# ------------------------------------------------------------------ backup

def backup_target_info() -> dict:
    """Source/dest/conflict folders the backup script will use, without running it."""
    info = {"script": BACKUP_SCRIPT.name, "available": BACKUP_SCRIPT.is_file(),
            "source": "", "dest": "", "conflicts": "", "online": False, "message": ""}
    if not info["available"]:
        info["message"] = f"{BACKUP_SCRIPT.name} not found next to sonar_web.py."
        return info
    try:
        mod = __import__(BACKUP_MODULE)
        if hasattr(mod, "resolve_paths"):
            ns = argparse.Namespace(config=None, source=None, dest=None, index=None, conflicts=None)
            src, dest, _index, conflicts, _cfg = mod.resolve_paths(ns)
        else:
            src, dest, conflicts = mod.SOURCE_ROOT, mod.DEST_ROOT, mod.CONFLICTS_ROOT
        info.update(source=str(src), dest=str(dest), conflicts=str(conflicts))
        drive = Path(dest).drive
        info["online"] = (not drive) or Path(drive + "\\").exists()
        if not info["online"]:
            info["message"] = f"Drive {drive} is not connected. Plug it in to back up."
    except SystemExit as exc:
        info["message"] = str(exc)
    except Exception as exc:
        info["message"] = f"Backup not configured: {exc}"
    return info


def _python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return sys.executable


def _supports_skip_pre_reindex() -> bool:
    try:
        return "--skip-pre-reindex" in BACKUP_SCRIPT.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _run_backup(apply: bool, reindex: bool, pre_reindex: bool) -> dict:
    info = backup_target_info()
    if not info["online"]:
        raise RuntimeError(info["message"] or "Backup destination is not connected.")
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = PLANS_DIR / f"backup-{'apply' if apply else 'dryrun'}-{stamp}.csv"
    cmd = [_python(), str(BACKUP_SCRIPT), "--log", str(log_path)]
    supports_skip = _supports_skip_pre_reindex()
    if not pre_reindex and supports_skip:
        cmd.append("--skip-pre-reindex")  # fast preview on the current indexes
    if apply:
        cmd.append("--apply")
        if reindex:
            cmd.append("--reindex")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=flags)
    with _LOCK:
        _CHILDREN.add(proc)
        _BACKUP_STATE["live_log"] = str(log_path)
    try:
        out_text, _ = proc.communicate()
    finally:
        with _LOCK:
            _CHILDREN.discard(proc)
            _BACKUP_STATE["live_log"] = ""
    proc.stdout_text = out_text or ""  # type: ignore[attr-defined]
    counts = {"copy": 0, "skip": 0, "conflict": 0, "error": 0}
    copy_bytes = 0
    rows: list[dict] = []
    src_root = Path(info["source"]) if info["source"] else None
    if log_path.is_file():
        with log_path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                action = (row.get("Action") or "").upper()
                kind = ("copy" if action.endswith("COPY") else "conflict" if action.endswith("CONFLICT")
                        else "error" if action == "ERROR" else "skip")
                counts[kind] += 1
                if kind == "skip":
                    continue
                src = Path(row.get("Source") or "")
                if kind == "copy":
                    try:
                        copy_bytes += src.stat().st_size
                    except OSError:
                        pass
                if len(rows) < 3000:
                    rel = str(src)
                    if src_root:
                        try:
                            rel = str(src.relative_to(src_root))
                        except ValueError:
                            pass
                    rows.append({"kind": kind, "rel": rel, "source": remember(src),
                                 "dest": row.get("Dest") or "", "reason": row.get("Reason") or ""})
        remember(log_path)
    output = proc.stdout_text.strip()  # type: ignore[attr-defined]
    _clear_caches()
    result = {"apply": apply, "pre_reindex": bool(pre_reindex and supports_skip), "code": proc.returncode, "counts": counts, "copy_bytes": copy_bytes,
              "rows": rows, "log": str(log_path) if log_path.is_file() else "",
              "output": output[-4000:], "finished": now_text(), "info": info}
    if not apply:
        _BACKUP_STATE["dry_run"] = result
    if not log_path.is_file() and proc.returncode != 0:
        raise RuntimeError(output[-800:] or f"{BACKUP_SCRIPT.name} exited {proc.returncode}")
    return result


def _external_backup_running() -> str:
    """Another sync/backup run started outside Sonar (e.g. from a terminal)? Return its PID text."""
    if os.name != "nt":
        return ""
    script = BACKUP_SCRIPT.name.replace("'", "")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{script}*' }} | ForEach-Object {{ $_.ProcessId }}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return ""
    with _LOCK:
        mine = {str(p.pid) for p in _CHILDREN}
    pids = [x.strip() for x in out.split() if x.strip().isdigit() and x.strip() not in mine]
    return ", ".join(pids)


def api_backup_run(body: dict) -> dict:
    apply = bool(body.get("apply"))
    other = _external_backup_running()
    if other:
        raise ValueError(f"{BACKUP_SCRIPT.name} is already running outside Sonar (PID {other}). "
                         "Wait for it to finish, then preview again.")
    if apply:
        dry = _BACKUP_STATE.get("dry_run")
        if not dry:
            raise ValueError("Run Preview backup first.")
    pre = bool(body.get("pre_reindex", apply))  # Preview: fast by default; Run: fresh indexes by default
    with _LOCK:
        busy = any(j["kind"].startswith("backup") and j["state"] == "running" for j in _JOBS.values())
    if busy:
        raise ValueError("A backup job is already running.")
    job = start_job("backup-apply" if apply else "backup",
                    lambda: _run_backup(apply, bool(body.get("reindex", True)), pre))
    return job_view(job)


# -------------------------------------------------------------- open/peek

def _check_path(raw: str) -> Path:
    if not raw or not is_known(raw):
        raise PermissionError("Sonar only opens files it listed in this session.")
    return Path(raw)


def api_open(body: dict) -> dict:
    path = _check_path(str(body.get("path") or ""))
    msg = offline_message(path)
    if msg:
        raise FileNotFoundError(msg)
    mode = body.get("mode") or "file"
    if mode == "folder":
        target = path if path.is_dir() else path.parent
        if not path.exists() and not target.exists():
            raise MissingFile(f"Folder not found (moved or renamed?): {target}")
        if os.name == "nt":
            if path.is_file():
                subprocess.Popen(["explorer", f"/select,{path}"])
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True}
    if not path.exists():
        raise MissingFile(f"Not at its indexed path (moved or renamed?): {path}")
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return {"ok": True}


class MissingFile(FileNotFoundError):
    """File is listed in the index but no longer at that path (moved or renamed)."""


def index_stamp(label: str) -> str:
    try:
        for st in S.source_statuses(load_cfg()):
            if not label or st.label == label or st.name == label:
                return st.indexed_at
    except Exception:
        pass
    return ""


def missing_info(label: str) -> dict:
    return {"missing": True, "drive": label, "indexed_at": index_stamp(label),
            "message": "This file is not at its indexed path. It may have been moved or renamed since the last index."}


def api_peek(body: dict) -> dict:
    path = _check_path(str(body.get("path") or ""))
    msg = offline_message(path)
    if msg:
        return {"offline": msg}
    try:
        st = path.stat()
    except OSError:
        return missing_info(str(body.get("drive") or ""))
    out = {"size": st.st_size, "modified": fmt_mtime(st.st_mtime_ns), "exists": True}
    if path.suffix.lower() in TEXT_EXTS and st.st_size < 50_000_000:
        try:
            with path.open("rb") as fh:
                out["text"] = fh.read(6000).decode("utf-8", errors="replace")
        except OSError:
            pass
    return out


def api_reindex(body: dict) -> dict:
    source = body.get("source") or "all"
    def run() -> dict:
        try:
            code, output = S.run_reindex(source=source, cfg=load_cfg())
        finally:
            _clear_caches()
        return {"code": code, "output": output[-4000:]}

    job = start_job("reindex", run)
    return job_view(job)


# ----------------------------------------------------------------- server

POST_ROUTES = {
    "/api/open": api_open,
    "/api/peek": api_peek,
    "/api/sort/preview": api_sort_preview,
    "/api/sort/apply": api_sort_apply,
    "/api/sort/undo": api_sort_undo,
    "/api/backup/run": api_backup_run,
    "/api/reindex": api_reindex,
    "/api/refresh-index": api_reindex,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "Sonar/2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet
        pass

    # -- plumbing
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in {"127.0.0.1", "localhost"}

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, MissingFile):
            return self._json(404, {"error": str(exc), "missing": True})
        code = 403 if isinstance(exc, PermissionError) else 400 if isinstance(exc, (ValueError, FileNotFoundError, RuntimeError)) else 500
        if code == 500:
            log(traceback.format_exc())
        self._json(code, {"error": str(exc)})

    # -- GET (read-only)
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        url = urllib.parse.urlsplit(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                html = UI_FILE.read_text(encoding="utf-8").replace("__SONAR_TOKEN__", TOKEN)
                csp = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                       "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": csp})
            if url.path == "/api/ping":
                return self._json(200, {"app": "sonar", "ok": True})
            if url.path == "/api/meta":
                return self._json(200, api_meta())
            if url.path == "/api/drives":
                return self._json(200, {"drives": source_status_list(), "backup": backup_target_info(),
                                        "checked": now_text()})
            if url.path == "/api/search":
                return self._json(200, api_search(qs))
            if url.path == "/api/expiring":
                return self._json(200, api_expiring(qs))
            if url.path == "/api/job":
                job = _JOBS.get(qs.get("id") or "")
                return self._json(200 if job else 404, job_view(job) if job else {"error": "no such job"})
            if url.path == "/api/backup/last":
                return self._json(200, {"dry_run": _BACKUP_STATE.get("dry_run"),
                                        "running": next((job_view(j) for j in _JOBS.values()
                                                         if j["kind"].startswith("backup") and j["state"] == "running"), None)})
            if url.path == "/api/image":
                # <img> cannot send headers, so the token rides in the query string.
                if not secrets.compare_digest(qs.get("t") or "", TOKEN):
                    return self._json(403, {"error": "token required"})
                path = _check_path(qs.get("p") or "")
                ctype = IMAGE_EXTS.get(path.suffix.lower())
                if not ctype or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                    return self._json(404, {"error": "no preview"})
                return self._send(200, path.read_bytes(), ctype)
            return self._json(404, {"error": "not found"})
        except Exception as exc:
            self._error(exc)

    do_HEAD = do_GET

    # -- POST (actions): token + same-origin required
    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).hostname not in {"127.0.0.1", "localhost"}:
            return self._json(403, {"error": "cross-origin request refused"})
        if not secrets.compare_digest(self.headers.get("X-Sonar-Token") or "", TOKEN):
            return self._json(403, {"error": "missing or wrong X-Sonar-Token"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/shutdown":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        fn = POST_ROUTES.get(path)
        if fn is None:
            return self._json(404, {"error": "not found"})
        try:
            self._json(200, fn(body))
        except Exception as exc:
            self._error(exc)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return  # browser closed the socket; not an error
        super().handle_error(request, client_address)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _existing_sonar(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.6) as resp:
            return json.loads(resp.read()).get("app") == "sonar"
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sonar modern web UI (local only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT}; falls back to a free one)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = parser.parse_args(argv)

    if not UI_FILE.is_file():
        log(f"Missing {UI_FILE.name} next to sonar_web.py")
        return 1
    port = args.port
    if not _port_free(port):
        if not args.no_browser and _existing_sonar(port):
            webbrowser.open(f"http://127.0.0.1:{port}/")
            log(f"Sonar is already running at http://127.0.0.1:{port}/")
            return 0
        port = 0  # let the OS pick
    server = QuietServer(("127.0.0.1", port), Handler)
    threading.Thread(target=prewarm, daemon=True).start()
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    log(f"Sonar web UI: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with _LOCK:
            children = list(_CHILDREN)
        for proc in children:  # a stopped Sonar must not leave a backup running unseen
            try:
                proc.terminate()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
