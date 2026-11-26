#!/usr/bin/env python3
"""Sonar: search the Family cabinet index from the command line or as a library.

Reads the SQLite FTS5 index built by sonar_index.py. Never writes to the
database. --reindex shells out to sonar_index.py index.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from sonar_index import (  # noqa: E402
    db_path,
    list_expiring,
    load_config,
    parse_day,
    search_index,
    split_filename,
)


def like_escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

_TERM_RE = re.compile(r'"([^"]*)"|(\S+)')


@dataclass(frozen=True)
class SearchHit:
    name: str
    folder: str
    snippet: str
    full_path: Path
    root: str
    rel_path: str
    source: str = ""


@dataclass(frozen=True)
class ExpiryItem:
    person: str
    name: str
    expiry: date
    days_left: int
    folder: str
    full_path: Path
    rel_path: str
    source: str = ""


ALL_SOURCE = "all"
ALL_SOURCE_LABEL = "All drives"


@dataclass(frozen=True)
class SearchSource:
    name: str
    label: str
    db: Path
    config_path: Path | None = None


def python_exe() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def enable_windows_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if os.name != "nt":
        return
    os.system("")
    try:
        kernel32 = ctypes.windll.kernel32
        enable_vt = 0x0004
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | enable_vt)
    except (AttributeError, OSError):
        pass


def root_map(cfg: dict) -> dict[str, Path]:
    return {item["name"]: Path(item["path"]) for item in cfg["roots"]}


def connect_ro(cfg: dict) -> sqlite3.Connection:
    path = db_path(cfg).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Index not found: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a.as_posix().lower() == b.as_posix().lower()


def disconnected_drive_message(path: Path | str | None) -> str | None:
    """If path is on a Windows drive that is not mounted, return a user message."""
    if path is None:
        return None
    candidate = Path(path)
    drive = candidate.drive
    if os.name == "nt" and len(drive) >= 2 and drive[1] == ":":
        letter = drive[:2].upper()
        try:
            ready = Path(letter + "/").exists()
        except OSError:
            ready = False
        if not ready:
            return (
                f"Drive {letter} is not connected. "
                "Plug in the drive to preview or open this file."
            )
    return None


def _expand_cfg_path(value: str | Path, cfg: dict) -> Path:
    try:
        from sonar_index import expand_path
        return expand_path(str(value))
    except Exception:
        raw = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if raw.is_absolute():
            return raw
        base = cfg.get("_config_path")
        root = Path(base).resolve().parent if base else Path.cwd()
        return (root / raw).resolve()


def configured_sources(cfg: dict | None = None) -> list[SearchSource]:
    """Return configured search sources. Always includes the primary index_db."""
    cfg = cfg or load_config(None)
    items = list(cfg.get("extra_indexes") or [])
    sources: list[SearchSource] = []
    for raw in items:
        db = _expand_cfg_path(raw.get("db") or raw.get("path") or "", cfg)
        name = str(raw.get("name") or raw.get("label") or db.stem).strip()
        label = str(raw.get("label") or raw.get("name") or db.stem).strip()
        config_path = None
        if raw.get("config"):
            config_path = _expand_cfg_path(raw["config"], cfg)
        sources.append(SearchSource(name=name, label=label, db=db, config_path=config_path))
    primary = db_path(cfg)
    if not any(_same_path(s.db, primary) for s in sources):
        label = "Local PC" if sources else "Local"
        sources.insert(
            0,
            SearchSource(name="local", label=label, db=primary, config_path=None),
        )
    return sources


def _normalize_source_key(value: str) -> str:
    key = value.strip().lower().replace("_", "-").replace(" ", "-")
    if key in {"all", "all-drives", "all-drive"}:
        return ALL_SOURCE
    if key in {"local", "local-pc", "pc", "cabinet"}:
        return "local"
    if key in {"external", "external-drive"}:
        return "external"
    return key


def resolve_sources(source: str | None, cfg: dict | None = None) -> list[SearchSource]:
    cfg = cfg or load_config(None)
    sources = configured_sources(cfg)
    if not source or _normalize_source_key(source) == ALL_SOURCE:
        return sources
    key = _normalize_source_key(source)
    wanted = [s for s in sources if _normalize_source_key(s.name) == key or _normalize_source_key(s.label) == key]
    if not wanted:
        choices = ", ".join([ALL_SOURCE] + [s.name for s in sources] + [s.label for s in sources])
        raise ValueError(f"Unknown source {source!r}. Choose one of: {choices}")
    return wanted


def cfg_for_source(source: SearchSource, main_cfg: dict) -> dict:
    if source.config_path and source.config_path.is_file():
        src_cfg = load_config(source.config_path)
    else:
        src_cfg = dict(main_cfg)
    src_cfg = dict(src_cfg)
    src_cfg["index_db"] = str(source.db)
    return src_cfg


def _roots_disconnected(cfg: dict) -> str | None:
    for item in cfg.get("roots") or []:
        path = item.get("path")
        if not path:
            continue
        message = disconnected_drive_message(path)
        if message:
            return message
    return None


def _note(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def display_folder(rel_path: str) -> str:
    parent = Path(rel_path).parent
    if parent.parts in ((), (".",)):
        return "."
    return str(parent).replace("/", "\\")


def _as_date(value: date | str | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return parse_day(str(value))


def _rows_to_hits(rows: list[dict], cfg: dict, source: str = "") -> list[SearchHit]:
    roots = root_map(cfg)
    hits: list[SearchHit] = []
    for row in rows:
        root_path = roots.get(row["root"])
        if root_path is None:
            continue
        rel_path = row["rel_path"]
        full_path = root_path / rel_path
        try:
            full_path = full_path.resolve()
        except OSError:
            pass
        hits.append(
            SearchHit(
                name=row["name"],
                folder=display_folder(rel_path),
                snippet=row.get("snippet") or display_folder(rel_path),
                full_path=full_path,
                root=row["root"],
                rel_path=rel_path,
                source=source,
            )
        )
    return hits


def parse_terms(text: str) -> list[str]:
    terms: list[str] = []
    for match in _TERM_RE.finditer(text.strip()):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if not raw or raw.upper() in {"AND", "OR", "NOT"}:
            continue
        term = raw[1:] if raw.startswith("-") and len(raw) > 1 else raw
        term = term.rstrip("*").strip()
        if term:
            terms.append(term)
    return terms


def _search_like(
    conn: sqlite3.Connection,
    terms: list[str],
    limit: int,
    folder: str | None,
    person: str | None,
    file_type: str | None,
) -> list[dict]:
    from sonar_index import _filter_sql

    if not terms:
        return []
    clauses: list[str] = []
    params: list = []
    for term in terms:
        pattern = "%" + like_escape(term) + "%"
        clauses.append(
            "(f.name LIKE ? ESCAPE '\\' OR f.rel_path LIKE ? ESCAPE '\\' "
            "OR COALESCE(f.split_name, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(f.body, '') LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern, pattern])
    extra_sql, extra_params = _filter_sql(folder, person, file_type)
    sql = f"""SELECT f.root, f.rel_path, f.name, f.size, f.mtime_ns, f.sha256,
                     f.name AS snippet
              FROM files f
              WHERE {' AND '.join(clauses)} {extra_sql}
              ORDER BY length(f.name), f.name
              LIMIT ?"""
    params.extend(extra_params)
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "root": row["root"],
            "rel_path": row["rel_path"],
            "name": row["name"],
            "size": row["size"],
            "mtime_ns": row["mtime_ns"],
            "sha256": row["sha256"],
            "snippet": split_filename(row["name"]) or row["name"],
        }
        for row in rows
    ]


def search(
    query: str,
    limit: int = 20,
    cfg: dict | None = None,
    folder: str | None = None,
    person: str | None = None,
    file_type: str | None = None,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    source: str | None = None,
    warnings: list[str] | None = None,
) -> tuple[list[SearchHit], str]:
    """Return (hits, mode) where mode is 'fts', 'like', or 'fts+like'.

    `source` selects one configured index or "all" (the default when
    extra_indexes are configured). Existing callers that omit it keep
    working: a single-index config searches only index_db; a multi-index
    config searches every source.
    """
    if not query or not query.strip():
        raise ValueError("search needs a query")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    cfg = cfg or load_config(None)
    sources = resolve_sources(source, cfg)
    all_hits: list[SearchHit] = []
    modes: list[str] = []
    for src in sources:
        src_cfg = cfg_for_source(src, cfg)
        if not Path(src_cfg["index_db"]).is_file():
            _note(warnings, f"{src.label}: index not found ({src.db}). Skipped.")
            continue
        try:
            hits, mode = _search_one(
                query,
                limit=limit,
                cfg=src_cfg,
                folder=folder,
                person=person,
                file_type=file_type,
                date_from=date_from,
                date_to=date_to,
                source_label=src.label,
            )
        except FileNotFoundError as exc:
            _note(warnings, f"{src.label}: {exc}")
            continue
        except (ValueError, sqlite3.Error) as exc:
            _note(warnings, f"{src.label}: {exc}")
            continue
        all_hits.extend(hits)
        modes.append(mode)
    if not all_hits and warnings and not sources:
        raise ValueError("No search sources are configured.")
    if "fts+like" in modes or ("fts" in modes and "like" in modes):
        mode = "fts+like"
    elif "fts" in modes:
        mode = "fts"
    else:
        mode = "like"
    return all_hits, mode


def _search_one(
    query: str,
    limit: int,
    cfg: dict,
    folder: str | None,
    person: str | None,
    file_type: str | None,
    date_from: date | str | None,
    date_to: date | str | None,
    source_label: str,
) -> tuple[list[SearchHit], str]:
    conn = connect_ro(cfg)
    try:
        try:
            fts_rows = search_index(
                conn,
                query,
                limit=limit,
                folder=folder or None,
                person=person or None,
                file_type=file_type or None,
                date_from=_as_date(date_from),
                date_to=_as_date(date_to),
            )
        except (ValueError, sqlite3.OperationalError):
            fts_rows = []
        terms = parse_terms(query)
        like_rows: list[dict] = []
        if len(fts_rows) < limit and terms:
            like_rows = _search_like(conn, terms, max(limit, 200), folder, person, file_type)
            start, end = _as_date(date_from), _as_date(date_to)
            if start or end:
                from sonar_index import _date_in_range, _mtime_date, doc_dates_from_name

                kept = []
                for row in like_rows:
                    mtime_ok = _date_in_range(_mtime_date(row["mtime_ns"]), start, end)
                    name_ok = any(_date_in_range(d, start, end) for d in doc_dates_from_name(row["name"]))
                    if mtime_ok or name_ok:
                        kept.append(row)
                like_rows = kept
        seen: set[tuple[str, str]] = set()
        rows: list[dict] = []
        for row in fts_rows:
            key = (row["root"], row["rel_path"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        for row in like_rows:
            key = (row["root"], row["rel_path"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= limit:
                break
        rows = rows[:limit]
        if fts_rows and len(rows) > len(fts_rows):
            mode = "fts+like"
        elif fts_rows:
            mode = "fts"
        else:
            mode = "like"
        return _rows_to_hits(rows, cfg, source=source_label), mode
    finally:
        conn.close()


def expiring(
    days: int | None = 180,
    cfg: dict | None = None,
    source: str | None = None,
    warnings: list[str] | None = None,
) -> list[ExpiryItem]:
    cfg = cfg or load_config(None)
    sources = resolve_sources(source, cfg)
    items: list[ExpiryItem] = []
    for src in sources:
        src_cfg = cfg_for_source(src, cfg)
        if not Path(src_cfg["index_db"]).is_file():
            _note(warnings, f"{src.label}: index not found ({src.db}). Skipped.")
            continue
        try:
            conn = connect_ro(src_cfg)
        except FileNotFoundError as exc:
            _note(warnings, f"{src.label}: {exc}")
            continue
        try:
            rows = list_expiring(conn, days=days, roots=root_map(src_cfg))
        except sqlite3.Error as exc:
            _note(warnings, f"{src.label}: {exc}")
            continue
        finally:
            conn.close()
        for row in rows:
            items.append(
                ExpiryItem(
                    person=row["person"],
                    name=row["name"],
                    expiry=row["expiry"],
                    days_left=row["days_left"],
                    folder=row["folder"],
                    full_path=Path(row["full_path"]),
                    rel_path=row["rel_path"],
                    source=src.label,
                )
            )
    return items


def run_reindex(source: str | None = None, cfg: dict | None = None) -> tuple[int, str]:
    """Reindex one source or all sources.

    With no arguments, reindexes the primary index_db (existing behavior).
    Pass source="all" or a source name/label to refresh configured sources.
    Sources whose root drive is unplugged are skipped with a message.
    """
    script = _SCRIPTS / "sonar_index.py"
    cfg = cfg or load_config(None)
    if source is None:
        sources = [SearchSource(name="local", label="Local PC", db=db_path(cfg), config_path=None)]
        use_source_cfg = False
    else:
        sources = resolve_sources(source, cfg)
        use_source_cfg = True

    chunks: list[str] = []
    any_ok = False
    any_attempted = False
    last_code = 0
    for src in sources:
        src_cfg = cfg_for_source(src, cfg) if use_source_cfg else cfg
        skipped = _roots_disconnected(src_cfg)
        if skipped:
            chunks.append(f"{src.label}: skipped. {skipped}")
            continue
        any_attempted = True
        cmd = [python_exe(), str(script)]
        if src.config_path and src.config_path.is_file():
            cmd.extend(["--config", str(src.config_path)])
        elif cfg.get("_config_path"):
            cmd.extend(["--config", str(cfg["_config_path"])])
        cmd.extend(["--db", str(src.db), "index"])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
        header = f"{src.label} ({src.db}):"
        block = header + ("\n" + output if output else "")
        if proc.returncode != 0:
            last_code = proc.returncode
            chunks.append(block or f"{header} failed with exit {proc.returncode}")
        else:
            any_ok = True
            chunks.append(block or f"{header} updated.")
    text_out = "\n\n".join(chunks).rstrip()
    if any_attempted and not any_ok and last_code != 0:
        return last_code, text_out
    return 0, text_out


def osc8(uri: str, label: str) -> str:
    return f"\x1b]8;;{uri}\x1b\\{label}\x1b]8;;\x1b\\"


def file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def format_hit(index: int, hit: SearchHit) -> str:
    file_link = osc8(file_uri(hit.full_path), "Open file")
    folder_link = osc8(file_uri(hit.full_path.parent), "Open folder")
    source_line = f"   Source:  {hit.source}\n" if hit.source else ""
    offline = disconnected_drive_message(hit.full_path)
    offline_line = f"\n   {offline}" if offline else ""
    return (
        f"{index}. {hit.name}\n"
        f"{source_line}"
        f"   Folder:  {hit.folder}\n"
        f"   Snippet: {hit.snippet}\n"
        f"   {file_link}  |  {folder_link}\n"
        f"   {hit.full_path}{offline_line}"
    )


def format_expiry(index: int, item: ExpiryItem) -> str:
    file_link = osc8(file_uri(item.full_path), "Open file")
    folder_link = osc8(file_uri(item.full_path.parent), "Open folder")
    when = "overdue" if item.days_left < 0 else f"{item.days_left} days left"
    source_line = f"   Source:  {item.source}\n" if item.source else ""
    offline = disconnected_drive_message(item.full_path)
    offline_line = f"\n   {offline}" if offline else ""
    return (
        f"{index}. {item.name}\n"
        f"{source_line}"
        f"   Person:  {item.person or '—'}\n"
        f"   Expiry:  {item.expiry.isoformat()}  ({when})\n"
        f"   Folder:  {item.folder}\n"
        f"   {file_link}  |  {folder_link}\n"
        f"   {item.full_path}{offline_line}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sonar: search the Family cabinet index (read-only)."
    )
    parser.add_argument("query", nargs="?", default="", help="words, \"phrase\", OR, -word, or prefix*")
    parser.add_argument("-n", "--limit", type=int, default=20, help="max results (default 20)")
    parser.add_argument("--folder", help="top-level Family folder, e.g. '40 Money'")
    parser.add_argument("--person", help="person token from config, e.g. Kid1")
    parser.add_argument("--type", dest="file_type", help="PDF, Image, Word, Excel, Text, Video, eBooks, or Other")
    parser.add_argument("--from", dest="date_from", help="start date YYYY-MM-DD (mtime or filename date)")
    parser.add_argument("--to", dest="date_to", help="end date YYYY-MM-DD")
    parser.add_argument("--expiring", action="store_true", help="list documents with Exp-YYYY-MM-DD in the name")
    parser.add_argument("--days", type=int, default=180, help="for --expiring, include past-due and due within N days")
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="run sonar_index.py index, then search if a query was given",
    )
    parser.add_argument("--config", help="settings JSON (default: config.json, else config.example.json)")
    parser.add_argument(
        "--source",
        default="all",
        help="search source: all (default), a configured name such as local or external, or a label",
    )
    return parser


def _print_warnings(warnings: list[str]) -> None:
    for message in warnings:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    enable_windows_output()
    args = build_parser().parse_args(argv)
    if args.config:
        os.environ["SONAR_CONFIG"] = str(Path(args.config).resolve())
    cfg = load_config(None)
    if args.reindex:
        code, output = run_reindex(source=args.source, cfg=cfg)
        if output:
            print(output)
        if code != 0:
            print(f"Reindex failed with exit code {code}", file=sys.stderr)
            return code
        if not args.query.strip() and not args.expiring:
            return 0
    if args.expiring:
        warnings: list[str] = []
        try:
            items = expiring(days=args.days, cfg=cfg, source=args.source, warnings=warnings)
        except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        _print_warnings(warnings)
        print(f"EXPIRING  within {args.days} days (and past due)  matches={len(items)}")
        if not items:
            print("    (none)")
            return 0
        for i, item in enumerate(items, 1):
            print()
            print(format_expiry(i, item))
        return 0
    if not args.query.strip():
        print("search needs a query", file=sys.stderr)
        return 2
    warnings = []
    try:
        hits, mode = search(
            args.query,
            limit=args.limit,
            cfg=cfg,
            folder=args.folder,
            person=args.person,
            file_type=args.file_type,
            date_from=args.date_from,
            date_to=args.date_to,
            source=args.source,
            warnings=warnings,
        )
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_warnings(warnings)
    suffix = "  (filename match; no FTS hits)" if mode == "like" else ""
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.source or "Local"] = counts.get(hit.source or "Local", 0) + 1
    breakdown = ", ".join(f"{name}: {n}" for name, n in counts.items())
    extra = f"  ({breakdown})" if len(counts) > 1 else ""
    print(f"SEARCH  {args.query!r}  matches={len(hits)}{suffix}{extra}")
    if not hits:
        print("    (none)")
        return 0
    for i, hit in enumerate(hits, 1):
        print()
        print(format_hit(i, hit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
