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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ExpiryItem:
    person: str
    name: str
    expiry: date
    days_left: int
    folder: str
    full_path: Path
    rel_path: str


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


def _rows_to_hits(rows: list[dict], cfg: dict) -> list[SearchHit]:
    roots = root_map(cfg)
    hits: list[SearchHit] = []
    for row in rows:
        root_path = roots.get(row["root"])
        if root_path is None:
            continue
        rel_path = row["rel_path"]
        hits.append(
            SearchHit(
                name=row["name"],
                folder=display_folder(rel_path),
                snippet=row.get("snippet") or display_folder(rel_path),
                full_path=(root_path / rel_path).resolve(),
                root=row["root"],
                rel_path=rel_path,
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
) -> tuple[list[SearchHit], str]:
    """Return (hits, mode) where mode is 'fts', 'like', or 'fts+like'."""
    if not query or not query.strip():
        raise ValueError("search needs a query")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    cfg = cfg or load_config(None)
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
        return _rows_to_hits(rows, cfg), mode
    finally:
        conn.close()


def expiring(days: int | None = 180, cfg: dict | None = None) -> list[ExpiryItem]:
    cfg = cfg or load_config(None)
    conn = connect_ro(cfg)
    try:
        rows = list_expiring(conn, days=days, roots=root_map(cfg))
    finally:
        conn.close()
    return [
        ExpiryItem(
            person=row["person"],
            name=row["name"],
            expiry=row["expiry"],
            days_left=row["days_left"],
            folder=row["folder"],
            full_path=Path(row["full_path"]),
            rel_path=row["rel_path"],
        )
        for row in rows
    ]


def run_reindex() -> tuple[int, str]:
    script = _SCRIPTS / "sonar_index.py"
    proc = subprocess.run(
        [python_exe(), str(script), "index"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
    return proc.returncode, output


def osc8(uri: str, label: str) -> str:
    return f"\x1b]8;;{uri}\x1b\\{label}\x1b]8;;\x1b\\"


def file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def format_hit(index: int, hit: SearchHit) -> str:
    file_link = osc8(file_uri(hit.full_path), "Open file")
    folder_link = osc8(file_uri(hit.full_path.parent), "Open folder")
    return (
        f"{index}. {hit.name}\n"
        f"   Folder:  {hit.folder}\n"
        f"   Snippet: {hit.snippet}\n"
        f"   {file_link}  |  {folder_link}\n"
        f"   {hit.full_path}"
    )


def format_expiry(index: int, item: ExpiryItem) -> str:
    file_link = osc8(file_uri(item.full_path), "Open file")
    folder_link = osc8(file_uri(item.full_path.parent), "Open folder")
    when = "overdue" if item.days_left < 0 else f"{item.days_left} days left"
    return (
        f"{index}. {item.name}\n"
        f"   Person:  {item.person or '—'}\n"
        f"   Expiry:  {item.expiry.isoformat()}  ({when})\n"
        f"   Folder:  {item.folder}\n"
        f"   {file_link}  |  {folder_link}\n"
        f"   {item.full_path}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sonar: search the Family cabinet index (read-only)."
    )
    parser.add_argument("--config", help="settings JSON (default: config.json, else config.example.json)")
    parser.add_argument("query", nargs="?", default="", help="words, \"phrase\", OR, -word, or prefix*")
    parser.add_argument("-n", "--limit", type=int, default=20, help="max results (default 20)")
    parser.add_argument("--folder", help="top-level Family folder, e.g. '40 Money'")
    parser.add_argument("--person", help="person token from config, e.g. Kid1")
    parser.add_argument("--type", dest="file_type", help="PDF, Image, Word, Excel, Text, Video, or Other")
    parser.add_argument("--from", dest="date_from", help="start date YYYY-MM-DD (mtime or filename date)")
    parser.add_argument("--to", dest="date_to", help="end date YYYY-MM-DD")
    parser.add_argument("--expiring", action="store_true", help="list documents with Exp-YYYY-MM-DD in the name")
    parser.add_argument("--days", type=int, default=180, help="for --expiring, include past-due and due within N days")
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="run sonar_index.py index, then search if a query was given",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    enable_windows_output()
    args = build_parser().parse_args(argv)
    if args.config:
        os.environ["SONAR_CONFIG"] = str(Path(args.config).resolve())
    if args.reindex:
        code, output = run_reindex()
        if output:
            print(output)
        if code != 0:
            print(f"Reindex failed with exit code {code}", file=sys.stderr)
            return code
        if not args.query.strip() and not args.expiring:
            return 0
    if args.expiring:
        try:
            items = expiring(days=args.days)
        except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
            print(str(exc), file=sys.stderr)
            return 1
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
    try:
        hits, mode = search(
            args.query,
            limit=args.limit,
            folder=args.folder,
            person=args.person,
            file_type=args.file_type,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    suffix = "  (filename match; no FTS hits)" if mode == "like" else ""
    print(f"SEARCH  {args.query!r}  matches={len(hits)}{suffix}")
    if not hits:
        print("    (none)")
        return 0
    for i, hit in enumerate(hits, 1):
        print()
        print(format_hit(i, hit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
