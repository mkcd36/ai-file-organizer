#!/usr/bin/env python3
"""Sonar indexer: read-only index of the Family cabinet.

Source files are never modified. The only write is the SQLite database at
<root>/_index/index.db (path comes from the config).

Commands:
  index    SHA-256 incremental update of configured roots
  search   FTS5 search over names, split names, paths, and extracted text
  audit    naming from AGENTS.md (file-naming), copy-pattern names, Exp-5555,
           expiries within 180 days, exact and possible duplicates.
           scripts/check.ps1 calls this for one folder.

Settings (cabinet root, people, db path, ...) come from config.json at the
project root, falling back to config.example.json. See load_settings().

Paths are stored relative to each root, with forward slashes.
A root's optional "include" list limits a plain `index` to those folders. An optional "exclude" list names directories to skip at any depth (in addition to built-in skips such as `_index` and `.git`).
Pass --folder to index or audit one folder explicitly.
Audit naming uses the live folder. Exact duplicates use index hashes when
the indexed size and mtime still match the file.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")  # replaced by settings["timezone"] in load_config()
PROJECT = Path(__file__).resolve().parent.parent
REPARSE_POINT = 0x400
CHUNK = 1024 * 1024

# Naming patterns are used only after AGENTS.md (file-naming section) still
# contains the templates below. Dated photos are enabled only when the README
# still describes them.
_PART = r"[A-Za-z0-9][A-Za-z0-9-]*"
_PERSON = r"[A-Z][A-Za-z0-9]+"
_PROJECT = r"[A-Z][A-Za-z0-9]+"
_DATE = r"\d{4}(?:-\d{2}(?:-\d{2})?)?"
_DATE_FULL = r"\d{4}-\d{2}-\d{2}"
_EXT = r"\.[a-z0-9]+"
RX_PERSON = re.compile(rf"^{_PERSON}_{_PART}(?:_{_DATE})?(?:_{_PART})*{_EXT}$")
RX_HOME = re.compile(rf"^{_PROJECT}_{_PART}(?:_{_DATE})?(?:_{_PART})*{_EXT}$")
RX_WORK = re.compile(rf"^{_PERSON}_{_PART}(?:_{_DATE})?(?:_{_PART})*{_EXT}$")
RX_TOPIC = re.compile(rf"^{_PROJECT}_{_PART}_{_PART}(?:_{_PART})*{_EXT}$")
RX_MONEY = re.compile(rf"^{_DATE_FULL}_{_PART}_{_PART}(?:_{_PART})*{_EXT}$")
RX_TAX = re.compile(rf"^\d{{4}}_Tax_{_PART}_{_PART}(?:_{_PART})*{_EXT}$")
RX_INSURANCE = re.compile(rf"^{_PERSON}_{_PART}_{_PART}{_EXT}$")
RX_PHOTO = re.compile(rf"^[A-Z][A-Za-z0-9-]+_{_PART}_\d{{2}}{_EXT}$")
RX_PHOTO_DATED = re.compile(rf"^{_PROJECT}_Photo_{_DATE_FULL}(?:_{_PART})?{_EXT}$")
RX_ID_EXP = re.compile(rf"^{_PERSON}(?:_{_PART})*_(?:Exp-|EXP_)(?:\d{{4}}-\d{{2}}-\d{{2}}|5555){_EXT}$")
RX_ID_OPEN = re.compile(rf"^{_PERSON}_{_PART}(?:_{_PART})*{_EXT}$")
RULES_READY = False
DATED_PHOTOS = False
RULES_LABEL = ""
RX_COPY = re.compile(
    r"\(\s*\d+\s*\)|(?<![A-Za-z])copy(?![A-Za-z])|_old|\.bak|Draft\d",
    re.IGNORECASE,
)
RX_VERSION = re.compile(
    r"\(\s*\d+\s*\)"
    r"|(?<![A-Za-z])copy(?![A-Za-z])"
    r"|_old\b"
    r"|\.bak(?:\.[A-Za-z0-9]+)?$"
    r"|Draft\d"
    r"|(?:^|[_\-\s])v\d{1,2}(?=[_.\-\s]|$)"
    r"|(?:^|[_\-\s])final(?:[\s_\-]?\d+)?(?=[_.\-\s]|$)"
    r"|[_-][bcd](?=\.[^.]+$)",
    re.IGNORECASE,
)
RX_VAGUE = re.compile(
    r"(?i)^(?:"
    r"scan|scans|scanned|document|documents|doc|docs|untitled|"
    r"img|image|images|photo|photos|picture|pictures|file|files|"
    r"new|temp|tmp|download|downloads|misc|miscellaneous|"
    r"book\d*|final|draft\d*|requested\s*record"
    r")(?:[\s_\-]*\d+)?$"
)
RX_CAMERA = re.compile(r"(?i)^(img|dsc|pict|photo)[-_ ]?\d+$")
RX_EXP5555 = re.compile(r"(?i)(?:Exp-|EXP_)5555")
RX_EXP_DATE = re.compile(r"(?i)(?:Exp-|EXP_)(\d{4}-\d{2}-\d{2})")

CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss",
    ".ps1", ".psm1", ".psd1", ".sh", ".bash",
    ".sql", ".rb", ".go", ".rs", ".java", ".cs",
    ".c", ".cpp", ".h", ".hpp", ".vue", ".svelte",
    ".json",
}
SKIP_NAMES = {"desktop.ini", "thumbs.db"}
SKIP_DIRS = {"_index", ".git", "System Volume Information", "$RECYCLE.BIN", "FOUND.000"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roots (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    root TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    split_name TEXT,
    body TEXT,
    content_status TEXT,
    content_error TEXT,
    content_chars INTEGER,
    indexed_at TEXT NOT NULL,
    UNIQUE(root, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    name,
    rel_path,
    split_name,
    body,
    content='files',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, name, rel_path, split_name, body)
    VALUES (new.id, new.name, new.rel_path, new.split_name, new.body);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, rel_path, split_name, body)
    VALUES ('delete', old.id, old.name, old.rel_path, old.split_name, old.body);
END;
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, rel_path, split_name, body)
    VALUES ('delete', old.id, old.name, old.rel_path, old.split_name, old.body);
    INSERT INTO files_fts(rowid, name, rel_path, split_name, body)
    VALUES (new.id, new.name, new.rel_path, new.split_name, new.body);
END;
"""


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def now_et() -> datetime:
    return datetime.now(ET)


DEFAULT_SETTINGS: dict = {
    "cabinet_root": "%USERPROFILE%\\Documents\\Family",
    "review_root": "%USERPROFILE%\\Documents\\To Review",
    "index_db": "",
    "exceptions": "plans\\check-exceptions.txt",
    "timezone": "America/New_York",
    "you": "You",
    "people": {
        "You": ["you"],
        "Spouse": ["spouse"],
        "Kid1": ["kid1"],
        "Kid2": ["kid2"],
        "Parents": ["parents"],
    },
    "children": ["Kid1", "Kid2"],
    "folders": [
        "00 Inbox", "10 Identity", "15 Travel", "20 Health", "30 Home", "40 Money",
        "50 Legal", "60 Work", "65 Learning", "70 Kids", "75 You", "80 Photos",
    ],
    "extra_roots": [],
    "ghostscript": "",
}


def expand_path(value: str) -> Path:
    """Expand %VAR%, $VAR and ~. %USERPROFILE% falls back to the home folder."""
    text = str(value)
    if "%USERPROFILE%" in text.upper() and not os.environ.get("USERPROFILE"):
        text = re.sub(r"(?i)%USERPROFILE%", str(Path.home()).replace("\\", "\\\\"), text)
    text = os.path.expanduser(os.path.expandvars(text))
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT / path
    return path


def settings_path(path: Path | None = None) -> Path:
    """config.json (yours, git-ignored) wins over config.example.json."""
    if path:
        return Path(path)
    env = os.environ.get("SONAR_CONFIG")
    if env:
        return Path(env)
    mine = PROJECT / "config.json"
    if mine.is_file():
        return mine
    return PROJECT / "config.example.json"


def load_settings(path: Path | None = None) -> dict:
    cfg_path = settings_path(path)
    data: dict = {}
    if cfg_path.is_file():
        with cfg_path.open(encoding="utf-8-sig") as fh:
            data = json.load(fh)
    elif path:
        raise SystemExit(f"Config not found: {cfg_path}")
    settings = {**DEFAULT_SETTINGS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    settings["_config_path"] = str(cfg_path)
    settings["cabinet_root"] = str(expand_path(settings["cabinet_root"]))
    settings["review_root"] = str(expand_path(settings["review_root"]))
    if not settings.get("index_db"):
        settings["index_db"] = str(Path(settings["cabinet_root"]) / "_index" / "index.db")
    settings["index_db"] = str(expand_path(settings["index_db"]))
    if settings.get("exceptions"):
        settings["exceptions"] = str(expand_path(settings["exceptions"]))
    you = settings.get("you") or "You"
    settings["you_folder"] = next(
        (f for f in settings.get("folders") or [] if f.startswith("75 ")), f"75 {you}"
    )
    return settings


def apply_settings(settings: dict) -> None:
    """Point module-level person and time-zone tables at the user's settings."""
    global ET, PERSON_ALIASES, PEOPLE, YOU_FOLDER
    try:
        ET = ZoneInfo(settings.get("timezone") or "America/New_York")
    except Exception:
        pass
    people = settings.get("people") or {}
    PERSON_ALIASES = {
        name: tuple(a.lower() for a in (aliases or [name.lower()])) for name, aliases in people.items()
    }
    PEOPLE = set(PERSON_ALIASES)
    YOU_FOLDER = settings.get("you_folder") or "75 You"


def load_config(path: Path | None) -> dict:
    """Index config built from settings: one 'cabinet' root plus any extra_roots."""
    settings = load_settings(path)
    apply_settings(settings)
    roots = [
        {
            "name": "cabinet",
            "path": settings["cabinet_root"],
            "include": list(settings.get("folders") or []),
        }
    ]
    for extra in settings.get("extra_roots") or []:
        item = dict(extra)
        item["path"] = str(expand_path(item["path"]))
        roots.append(item)
    extras = []
    for raw in settings.get("extra_indexes") or []:
        item = dict(raw)
        if item.get("db"):
            item["db"] = str(expand_path(item["db"]))
        if item.get("path"):
            item["path"] = str(expand_path(item["path"]))
        if item.get("config"):
            item["config"] = str(expand_path(item["config"]))
        extras.append(item)
    cfg = {
        "index_db": settings["index_db"],
        "exceptions": settings.get("exceptions") or "",
        "roots": roots,
        "settings": settings,
        "extra_indexes": extras,
        "_config_path": settings["_config_path"],
    }
    return cfg


def db_path(cfg: dict) -> Path:
    return Path(cfg["index_db"])


def connect(cfg: dict) -> sqlite3.Connection:
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def is_reparse(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & REPARSE_POINT:
        return True
    return stat.S_ISLNK(st.st_mode)


def skip_file_name(name: str) -> bool:
    low = name.lower()
    if low in SKIP_NAMES or low.endswith(".lnk") or name.startswith("~$"):
        return True
    return False


def rel_posix(root: Path, full: Path) -> str:
    return full.relative_to(root).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()



FTS_VERSION = "2"
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_TEXT_CHARS = 200 * 1024
MAX_PDF_PAGES = 50

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff", ".ico", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".mpeg", ".mpg", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".opus"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso"}
BINARY_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS | ARCHIVE_EXTS | {
    ".exe", ".msi", ".dll", ".sys", ".bin", ".dat", ".obj", ".pdb",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".psd", ".ai", ".eps", ".crdownload",
}
TEXT_EXTS = {".txt", ".csv", ".md", ".json", ".srt", ".vtt", ".log", ".html", ".htm", ".xml", ".yml", ".yaml"}
TYPE_GROUPS = {
    "PDF": {".pdf"},
    "Image": IMAGE_EXTS,
    "Word": {".doc", ".docx"},
    "Excel": {".xls", ".xlsx", ".xlsm"},
    "Text": TEXT_EXTS,
    "Video": VIDEO_EXTS,
}
# Filled from settings["people"] by apply_settings().
PERSON_ALIASES: dict[str, tuple[str, ...]] = {
    "You": ("you",), "Spouse": ("spouse",), "Kid1": ("kid1",), "Kid2": ("kid2",), "Parents": ("parents",),
}
PEOPLE: set[str] = set(PERSON_ALIASES)
YOU_FOLDER = "75 You"
RX_DOC_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
RX_DOC_MONTH = re.compile(r"(20\d{2})-(\d{2})(?!-\d{2})(?!\d)")
RX_EXPIRY = re.compile(r"(?i)(?:Exp-|EXP_)(\d{4}-\d{2}-\d{2})")
RX_CAMEL = re.compile(r"([a-z])([A-Z])")
RX_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
RX_LET_NUM = re.compile(r"([A-Za-z])(\d)")
RX_NUM_LET = re.compile(r"(\d)([A-Za-z])")

FTS_SQL = """
DROP TRIGGER IF EXISTS files_ai;
DROP TRIGGER IF EXISTS files_ad;
DROP TRIGGER IF EXISTS files_au;
DROP TABLE IF EXISTS files_fts;
CREATE VIRTUAL TABLE files_fts USING fts5(
    name,
    rel_path,
    split_name,
    body,
    content='files',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, name, rel_path, split_name, body)
    VALUES (new.id, new.name, new.rel_path, new.split_name, new.body);
END;
CREATE TRIGGER files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, rel_path, split_name, body)
    VALUES ('delete', old.id, old.name, old.rel_path, old.split_name, old.body);
END;
CREATE TRIGGER files_au AFTER UPDATE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, rel_path, split_name, body)
    VALUES ('delete', old.id, old.name, old.rel_path, old.split_name, old.body);
    INSERT INTO files_fts(rowid, name, rel_path, split_name, body)
    VALUES (new.id, new.name, new.rel_path, new.split_name, new.body);
END;
"""


def split_filename(name: str) -> str:
    stem = Path(name).stem
    text = re.sub(r"[_\-.]+", " ", stem)
    text = RX_CAMEL.sub(r"\1 \2", text)
    text = RX_ACRONYM.sub(r"\1 \2", text)
    text = RX_LET_NUM.sub(r"\1 \2", text)
    text = RX_NUM_LET.sub(r"\1 \2", text)
    ext = Path(name).suffix.lower().lstrip(".")
    words = " ".join(text.split())
    if ext:
        words = f"{words} {ext}".strip()
    return words


def _cap_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS]
    return text.strip()


def skip_reason(rel_path: str, ext: str, size: int) -> str | None:
    if size > MAX_FILE_BYTES:
        return "too large"
    posix = rel_path.replace("\\", "/")
    if ext == ".txt" and re.search(r"(?i)(serial|password|passwd|credential|license)", Path(posix).name):
        return "possible secret"
    if ext in BINARY_EXTS:
        return "binary"
    return None


def extract_text(path: Path, ext: str) -> tuple[str, str, str]:
    """Return (text, status, error). status is ok, empty, skip, or error."""
    try:
        if ext == ".pdf":
            text = _extract_pdf(path)
        elif ext == ".docx":
            text = _extract_docx(path)
        elif ext in {".xlsx", ".xlsm"}:
            text = _extract_xlsx(path)
        elif ext == ".pptx":
            text = _extract_pptx(path)
        elif ext in TEXT_EXTS:
            text = _extract_text_file(path)
        else:
            return "", "skip", "unsupported type"
    except Exception as exc:
        return "", "error", str(exc)[:300]
    text = _cap_text(text)
    if not text:
        return "", "empty", ""
    return text, "ok", ""


def _extract_pdf(path: Path) -> str:
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf  # type: ignore
    doc = pymupdf.open(path)
    try:
        parts: list[str] = []
        total = 0
        pages = min(int(doc.page_count), MAX_PDF_PAGES)
        for i in range(pages):
            piece = doc.load_page(i).get_text("text") or ""
            parts.append(piece)
            total += len(piece)
            if total >= MAX_TEXT_CHARS:
                break
        return "\n".join(parts)
    finally:
        doc.close()


def _extract_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text]
    for table in doc.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
        if sum(len(p) for p in parts) >= MAX_TEXT_CHARS:
            break
    return "\n".join(parts)


def _extract_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        total = 0
        for sheet in wb.worksheets:
            parts.append(f"# {sheet.title}")
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= 200:
                    break
                line = "\t".join("" if c is None else str(c) for c in row).rstrip()
                if line:
                    parts.append(line)
                    total += len(line)
                if total >= MAX_TEXT_CHARS:
                    return "\n".join(parts)
        return "\n".join(parts)
    finally:
        wb.close()


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        return ""
    prs = Presentation(str(path))
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text
                if text and text.strip():
                    parts.append(text)
        if sum(len(p) for p in parts) >= MAX_TEXT_CHARS:
            break
    return "\n".join(parts)


def _extract_text_file(path: Path) -> str:
    data = path.read_bytes()[: MAX_TEXT_CHARS * 2]
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    for name, typ in (
        ("split_name", "TEXT"),
        ("body", "TEXT"),
        ("content_status", "TEXT"),
        ("content_error", "TEXT"),
        ("content_chars", "INTEGER"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE files ADD COLUMN {name} {typ}")
    version = conn.execute("SELECT value FROM meta WHERE key='fts_version'").fetchone()
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='files_fts'").fetchone()
    sql = row[0] if row and row[0] else ""
    needs_fts = (
        version is None
        or version[0] != FTS_VERSION
        or "split_name" not in sql
        or "body" not in sql
    )
    # Drop FTS triggers first so split_name backfill cannot touch a half-built index.
    conn.execute("DROP TRIGGER IF EXISTS files_ai")
    conn.execute("DROP TRIGGER IF EXISTS files_ad")
    conn.execute("DROP TRIGGER IF EXISTS files_au")
    if needs_fts:
        conn.execute("DROP TABLE IF EXISTS files_fts")
        conn.commit()
    missing = conn.execute(
        "SELECT COUNT(*) FROM files WHERE split_name IS NULL OR split_name=''"
    ).fetchone()[0]
    if missing:
        for file_id, name in conn.execute("SELECT id, name FROM files"):
            conn.execute("UPDATE files SET split_name=? WHERE id=?", (split_filename(name), file_id))
        conn.commit()
    conn.executescript(FTS_SQL)
    conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('fts_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (FTS_VERSION,),
    )
    conn.commit()


def person_from_name(name: str) -> str | None:
    lower = name.lower()
    for person, aliases in PERSON_ALIASES.items():
        for alias in aliases:
            if re.match(rf"{re.escape(alias)}(?![a-z0-9])", lower):
                return person
    return None


def doc_dates_from_name(name: str) -> list[date]:
    found: list[date] = []
    for match in RX_DOC_DATE.finditer(name):
        try:
            found.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            continue
    for match in RX_DOC_MONTH.finditer(name):
        try:
            found.append(date(int(match.group(1)), int(match.group(2)), 1))
        except ValueError:
            continue
    return found


def file_type_label(ext: str) -> str:
    ext = ext.lower()
    for label, exts in TYPE_GROUPS.items():
        if ext in exts:
            return label
    return "Other"


def parse_day(value: str) -> date:
    text = value.strip()
    return date.fromisoformat(text)


def _date_in_range(day: date, start: date | None, end: date | None) -> bool:
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def _mtime_date(mtime_ns: int) -> date:
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, ET).date()


_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def fts_query(text: str) -> str:
    """words=AND, \"phrase\", OR, -word, prefix*."""
    text = text.strip()
    if not text:
        raise ValueError("search needs a query")
    parts: list[str] = []
    pending_or = False
    for match in _TOKEN.finditer(text):
        quoted = match.group(1)
        if quoted is not None:
            phrase = " ".join(quoted.split())
            token = f'"{phrase}"' if phrase else ""
        else:
            raw = match.group(2)
            upper = raw.upper()
            if upper == "AND":
                continue
            if upper == "OR":
                pending_or = True
                continue
            if upper == "NOT":
                parts.append("NOT")
                continue
            if raw.startswith("-") and len(raw) > 1:
                parts.append("NOT")
                raw = raw[1:]
            if raw.endswith("*") and len(raw) > 1:
                stem = raw[:-1].replace('"', " ").strip()
                token = f"{stem}*" if " " not in stem else f'"{stem}"*'
            else:
                cleaned = raw.replace('"', " ").strip()
                token = f'"{cleaned}"' if cleaned else ""
        if token:
            if pending_or and parts:
                parts.append("OR")
            parts.append(token)
            pending_or = False
    if not parts:
        raise ValueError("search needs a query")
    return " ".join(parts)


def _filter_sql(folder: str | None, person: str | None, file_type: str | None) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if folder:
        folder = folder.replace("\\", "/").strip("/")
        escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(f.rel_path = ? OR f.rel_path LIKE ? ESCAPE '\\')")
        params.extend([folder, escaped + "/%"])
    if person:
        key = person.strip()
        aliases: tuple[str, ...] = ()
        for name, al in PERSON_ALIASES.items():
            if key.lower() == name.lower() or key.lower() in al:
                aliases = (name.lower(),) + al
                break
        if not aliases:
            aliases = (key.lower(),)
        person_clauses = []
        for alias in aliases:
            esc = alias.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            person_clauses.append("(lower(f.name) LIKE ? ESCAPE '\\' OR lower(f.name) LIKE ? ESCAPE '\\')")
            params.extend([esc + "\\_%", esc + " %"])
        clauses.append("(" + " OR ".join(person_clauses) + ")")
    if file_type:
        label = file_type.strip()
        if label.lower() in {"ebooks", "ebook", "e-books", "e-book"}:
            # Any EPUB/Kindle book, plus PDFs only when they live under a Books
            # or Kindle Books folder, so statements and receipts stay out.
            clauses.append(
                "("
                "lower(f.ext) IN (?, ?, ?, ?) OR ("
                "lower(f.ext) = '.pdf' AND ("
                "replace(lower(f.rel_path), '\\', '/') LIKE '%/books/%' "
                "OR replace(lower(f.rel_path), '\\', '/') LIKE 'books/%' "
                "OR replace(lower(f.rel_path), '\\', '/') LIKE '%/kindle books/%' "
                "OR replace(lower(f.rel_path), '\\', '/') LIKE 'kindle books/%'"
                ")))"
            )
            params.extend([".epub", ".mobi", ".azw3", ".azw"])
        else:
            match = None
            for name, exts in TYPE_GROUPS.items():
                if label.lower() == name.lower():
                    match = exts
                    break
            if match is None and label.lower() == "other":
                known = set().union(*TYPE_GROUPS.values())
                placeholders = ",".join("?" * len(known))
                clauses.append(f"lower(f.ext) NOT IN ({placeholders})")
                params.extend(sorted(known))
            elif match:
                placeholders = ",".join("?" * len(match))
                clauses.append(f"lower(f.ext) IN ({placeholders})")
                params.extend(sorted(match))
    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def search_index(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    folder: str | None = None,
    person: str | None = None,
    file_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    fts = fts_query(query)
    extra_sql, extra_params = _filter_sql(folder, person, file_type)
    fetch_limit = max(limit * 20, 200) if (date_from or date_to) else limit
    sql = f"""
        SELECT f.root, f.rel_path, f.name, f.size, f.mtime_ns, f.sha256,
               bm25(files_fts, 12.0, 4.0, 8.0, 1.0) AS score,
               COALESCE(
                   NULLIF(snippet(files_fts, 3, '[', ']', '…', 12), ''),
                   NULLIF(snippet(files_fts, 2, '[', ']', '…', 8), ''),
                   NULLIF(snippet(files_fts, 0, '[', ']', '…', 8), ''),
                   f.name
               ) AS snippet
        FROM files_fts
        JOIN files f ON f.id = files_fts.rowid
        WHERE files_fts MATCH ? {extra_sql}
        ORDER BY score
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (fts, *extra_params, fetch_limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    hits: list[dict] = []
    for row in rows:
        if date_from or date_to:
            mtime_ok = _date_in_range(_mtime_date(row["mtime_ns"]), date_from, date_to)
            name_ok = any(_date_in_range(d, date_from, date_to) for d in doc_dates_from_name(row["name"]))
            if not (mtime_ok or name_ok):
                continue
        snippet = (row["snippet"] or "").replace("\n", " ")
        snippet = re.sub(r"\s+", " ", snippet).strip()
        hits.append(
            {
                "root": row["root"],
                "rel_path": row["rel_path"],
                "name": row["name"],
                "size": row["size"],
                "mtime_ns": row["mtime_ns"],
                "sha256": row["sha256"],
                "score": row["score"],
                "snippet": snippet,
            }
        )
        if len(hits) >= limit:
            break
    return hits


def list_expiring(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    roots: dict[str, Path] | None = None,
) -> list[dict]:
    today = now_et().date()
    items: list[dict] = []
    for row in conn.execute("SELECT root, rel_path, name, mtime_ns FROM files"):
        match = RX_EXPIRY.search(row["name"])
        if not match:
            continue
        raw = match.group(1)
        if raw == "5555":
            continue
        try:
            expiry = date.fromisoformat(raw)
        except ValueError:
            continue
        left = (expiry - today).days
        if days is not None and left > days:
            continue
        rel = row["rel_path"]
        root_path = (roots or {}).get(row["root"])
        full = (root_path / rel) if root_path else Path(rel)
        items.append(
            {
                "person": person_from_name(row["name"]) or "",
                "name": row["name"],
                "expiry": expiry,
                "days_left": left,
                "root": row["root"],
                "rel_path": rel,
                "folder": str(Path(rel).parent).replace("/", "\\") if Path(rel).parent.parts not in ((), (".",)) else ".",
                "full_path": full,
            }
        )
    items.sort(key=lambda item: (item["expiry"], item["name"]))
    return items



def like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/%"


def selected_roots(cfg: dict, root_name: str | None) -> list[dict]:
    if root_name and re.match(r"^[A-Za-z]:[\\/]", root_name):
        path = Path(root_name)
        label = re.sub(r"[^A-Za-z0-9]+", "-", path.as_posix()).strip("-").lower() or "root"
        return [{"name": label, "path": str(path)}]
    roots = cfg["roots"]
    if root_name:
        roots = [r for r in roots if r["name"] == root_name]
        if not roots:
            raise SystemExit(f"Unknown root: {root_name}")
    return roots


def prefixes_for(root_cfg: dict, folders: list[str] | None) -> list[str] | None:
    if folders:
        return [f.replace("\\", "/").strip("/") for f in folders]
    include = root_cfg.get("include")
    if include:
        return [str(item).replace("\\", "/").strip("/") for item in include]
    return None


def walk_prefix(root: Path, prefix: str | None, errors: list[str], root_cfg: dict | None = None):
    base = root if prefix is None else root / prefix
    if not base.exists():
        errors.append(f"missing folder: {base}")
        return
    if not base.is_dir():
        errors.append(f"not a folder: {base}")
        return
    protected: list[str] = []

    def onerror(err: OSError) -> None:
        errors.append(f"{err.filename}: {err.strerror}")
        if err.filename:
            try:
                protected.append(rel_posix(root, Path(err.filename)))
            except ValueError:
                pass

    skip = {name.lower() for name in SKIP_DIRS}
    if root_cfg:
        skip.update(
            str(item).replace("\\", "/").strip("/").lower()
            for item in (root_cfg.get("exclude") or [])
            if str(item).strip()
        )

    for dirpath, dirnames, filenames in os.walk(base, followlinks=False, onerror=onerror):
        kept: list[str] = []
        for dirname in dirnames:
            if dirname.lower() in skip:
                continue
            child = Path(dirpath) / dirname
            try:
                st = child.lstat()
            except OSError as exc:
                errors.append(f"{child}: {exc.strerror}")
                continue
            if is_reparse(st):
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            if skip_file_name(filename):
                continue
            full = Path(dirpath) / filename
            try:
                st = full.lstat()
            except OSError as exc:
                errors.append(f"{full}: {exc.strerror}")
                continue
            if is_reparse(st) or not stat.S_ISREG(st.st_mode):
                continue
            try:
                rel = rel_posix(root, full)
            except ValueError as exc:
                errors.append(f"{full}: {exc}")
                continue
            yield rel, full, st, protected


def index_root(
    conn: sqlite3.Connection,
    root_cfg: dict,
    folders: list[str] | None,
    *,
    hash_only: bool = False,
    size_prefilter: bool = False,
) -> dict:
    if hash_only or size_prefilter:
        conn.execute("PRAGMA synchronous=NORMAL")
    if hash_only:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS files_ai;
            DROP TRIGGER IF EXISTS files_ad;
            DROP TRIGGER IF EXISTS files_au;
            """
        )
    root = Path(root_cfg["path"]).resolve()
    name = root_cfg["name"]
    prefixes = prefixes_for(root_cfg, folders)
    errors: list[str] = []
    counts = {
        "seen": 0,
        "hashed": 0,
        "unchanged": 0,
        "removed": 0,
        "errors": 0,
        "extracted": 0,
        "extract_skipped": 0,
        "extract_empty": 0,
        "extract_errors": 0,
        "unique_size": 0,
    }
    conn.execute(
        "INSERT INTO roots(name, path) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET path=excluded.path",
        (name, str(root)),
    )
    targets = prefixes if prefixes is not None else [None]
    if not folders and prefixes is not None:
        # Folders from the config's include list are optional: skip ones you don't have.
        targets = [p for p in targets if p is None or (root / p).is_dir()]
    for prefix in targets:
        seen: set[str] = set()
        protected: list[str] = []
        staged: list[tuple] = []
        size_counts: dict[int, int] = defaultdict(int)
        for rel, full, st, protected in walk_prefix(root, prefix, errors, root_cfg):
            seen.add(rel)
            if size_prefilter:
                staged.append((rel, full, st))
                size_counts[st.st_size] += 1
            else:
                _index_one(conn, name, rel, full, st, None, counts, errors, hash_only, False)
        if size_prefilter:
            for rel, full, st in staged:
                shared = size_counts[st.st_size] > 1
                _index_one(conn, name, rel, full, st, shared, counts, errors, hash_only, True)
        removed = delete_stale(conn, name, prefix, seen, protected)
        counts["removed"] += removed
        conn.commit()
    counts["errors"] = len(errors)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_indexed', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (now_et().isoformat(timespec="seconds"),),
    )
    if hash_only:
        conn.executescript(FTS_SQL)
        conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
    conn.commit()
    return {"root": name, "path": str(root), "prefixes": prefixes or ["<entire root>"], **counts, "error_lines": errors}


def _index_one(
    conn: sqlite3.Connection,
    name: str,
    rel: str,
    full: Path,
    st: os.stat_result,
    shared_size: bool | None,
    counts: dict,
    errors: list[str],
    hash_only: bool,
    size_prefilter: bool,
) -> None:
    counts["seen"] += 1
    ext = full.suffix.lower()
    row = conn.execute(
        """SELECT id, size, mtime_ns, sha256, content_status
           FROM files WHERE root=? AND rel_path=?""",
        (name, rel),
    ).fetchone()
    same_stat = bool(row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns)
    if size_prefilter and shared_size is False:
        if same_stat and row["content_status"] == "unique-size":
            counts["unchanged"] += 1
            counts["unique_size"] += 1
            return
        digest = None
        body, status, err, chars = "", "unique-size", "size prefilter", 0
        counts["unique_size"] += 1
        counts["extract_skipped"] += 1
    else:
        unchanged_hash = bool(same_stat and row and row["sha256"])
        has_content = bool(row and row["content_status"])
        if unchanged_hash and (has_content or hash_only):
            counts["unchanged"] += 1
            return
        if unchanged_hash:
            digest = row["sha256"]
        else:
            try:
                digest = sha256_file(full)
            except OSError as exc:
                errors.append(f"{full}: {exc.strerror}")
                return
            counts["hashed"] += 1
        if hash_only:
            body, status, err, chars = "", "skip", "hash-only", 0
            counts["extract_skipped"] += 1
        else:
            reason = skip_reason(rel, ext, st.st_size)
            if reason:
                body, status, err, chars = "", "skip", reason, 0
                counts["extract_skipped"] += 1
            else:
                body, status, err = extract_text(full, ext)
                chars = len(body)
                if status == "ok":
                    counts["extracted"] += 1
                elif status == "empty":
                    counts["extract_empty"] += 1
                elif status == "error":
                    counts["extract_errors"] += 1
                    errors.append(f"{full}: extract: {err}")
                else:
                    counts["extract_skipped"] += 1
    split = split_filename(full.name)
    indexed_at = now_et().isoformat(timespec="seconds")
    if row:
        conn.execute(
            """UPDATE files
               SET name=?, ext=?, size=?, mtime_ns=?, sha256=?, indexed_at=?,
                   split_name=?, body=?, content_status=?, content_error=?, content_chars=?
               WHERE id=?""",
            (
                full.name, ext, st.st_size, st.st_mtime_ns, digest, indexed_at,
                split, body, status, err, chars, row["id"],
            ),
        )
    else:
        conn.execute(
            """INSERT INTO files(
                   root, rel_path, name, ext, size, mtime_ns, sha256, indexed_at,
                   split_name, body, content_status, content_error, content_chars
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, rel, full.name, ext, st.st_size, st.st_mtime_ns, digest, indexed_at,
                split, body, status, err, chars,
            ),
        )
    if counts["seen"] % 500 == 0:
        conn.commit()
        print(
            f"    ... seen={counts['seen']} hashed={counts['hashed']} unique_size={counts['unique_size']}",
            flush=True,
        )


def delete_stale(conn: sqlite3.Connection, root_name: str, prefix: str | None, seen: set[str], protected: list[str]) -> int:
    if prefix is None:
        rows = conn.execute("SELECT id, rel_path FROM files WHERE root=?", (root_name,)).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, rel_path FROM files
               WHERE root=? AND (rel_path=? OR rel_path LIKE ? ESCAPE '\\')""",
            (root_name, prefix, like_prefix(prefix)),
        ).fetchall()
    gone = []
    for row in rows:
        rel = row["rel_path"]
        if rel in seen:
            continue
        if any(rel == p or rel.startswith(p + "/") for p in protected):
            continue
        gone.append(row["id"])
    for file_id in gone:
        conn.execute("DELETE FROM files WHERE id=?", (file_id,))
    return len(gone)


def cmd_index(cfg: dict, args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        reports = []
        for root_cfg in selected_roots(cfg, args.root):
            reports.append(
                index_root(
                    conn,
                    root_cfg,
                    args.folder,
                    hash_only=bool(getattr(args, "hash_only", False)),
                    size_prefilter=bool(getattr(args, "size_prefilter", False)),
                )
            )
    finally:
        conn.close()
    for report in reports:
        prefixes = ", ".join(report["prefixes"])
        print(f"INDEX  {report['root']}  {report['path']}")
        print(f"folders: {prefixes}")
        print(
            "seen={seen}  hashed={hashed}  unique_size={unique_size}  unchanged={unchanged}  removed={removed}  errors={errors}".format(
                **report
            )
        )
        print(
            "content extracted={extracted}  empty={extract_empty}  "
            "skipped={extract_skipped}  errors={extract_errors}".format(**report)
        )
        for line in report["error_lines"][:20]:
            print(f"    ERROR  {line}")
        if len(report["error_lines"]) > 20:
            print(f"    ... {len(report['error_lines']) - 20} more errors")
    print(f"db: {db_path(cfg)}")
    return 1 if any(r["errors"] for r in reports) else 0


def cmd_search(cfg: dict, args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        date_from = parse_day(args.date_from) if getattr(args, "date_from", None) else None
        date_to = parse_day(args.date_to) if getattr(args, "date_to", None) else None
        hits = search_index(
            conn,
            args.query,
            limit=args.limit,
            folder=getattr(args, "folder", None),
            person=getattr(args, "person", None),
            file_type=getattr(args, "file_type", None),
            date_from=date_from,
            date_to=date_to,
        )
    finally:
        conn.close()
    print(f"SEARCH  {args.query!r}  matches={len(hits)}")
    if not hits:
        print("    (none)")
        return 0
    for row in hits:
        digest = (row["sha256"] or "")[:12]
        snippet = row.get("snippet") or ""
        print(f"    {row['root']}  {row['rel_path']}  {row['size']}  {digest}")
        if snippet:
            print(f"        {snippet}")
    return 0


class Exceptions:
    def __init__(self) -> None:
        self.prefixes: list[str] = []
        self.names: list[str] = []
        self.globs: list[str] = []
        self.regexes: list[re.Pattern[str]] = []
        self.kind_code = True
        self.loaded = False
        self.path = ""

    def load(self, path: str | None) -> None:
        self.path = path or ""
        if not path or not Path(path).is_file():
            return
        self.loaded = True
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            kind, _, rest = line.partition(":")
            kind = kind.strip().lower()
            rest = rest.strip().strip('"')
            if kind == "prefix":
                self.prefixes.append(rest.replace("/", "\\").rstrip("\\"))
            elif kind == "name":
                self.names.append(rest)
            elif kind == "glob":
                self.globs.append(rest)
            elif kind == "regex":
                self.regexes.append(re.compile(rest))
            elif kind == "kind" and rest.lower() == "code":
                self.kind_code = True


def is_excepted(exc: Exceptions, rel: str, name: str) -> bool:
    rel_win = rel.replace("/", "\\")
    for prefix in exc.prefixes:
        if rel_win.lower() == prefix.lower() or rel_win.lower().startswith(prefix.lower() + "\\"):
            return True
    for token in exc.names:
        if token.lower() in name.lower():
            return True
    for pattern in exc.globs:
        if fnmatch.fnmatch(rel_win.lower(), pattern.lower()):
            return True
    for pattern in exc.regexes:
        if pattern.search(rel_win):
            return True
    return False


def is_code(name: str) -> bool:
    return Path(name).suffix.lower() in CODE_EXTS


def naming_paths(cfg: dict) -> tuple[Path, Path]:
    """Rules file = AGENTS.md at the project root; README = cabinet README, else the kit README."""
    rules = PROJECT / "AGENTS.md"
    readme = None
    for root_cfg in cfg.get("roots", []):
        if root_cfg.get("name") == "cabinet":
            readme = Path(root_cfg["path"]) / "README.md"
            break
    if readme is None or not readme.is_file():
        readme = PROJECT / "README.md"
    return rules, readme


def load_naming_rules(mdc: Path, readme: Path) -> None:
    """Use the patterns only if AGENTS.md still states them."""
    global RULES_READY, DATED_PHOTOS, RULES_LABEL
    if not mdc.is_file():
        raise SystemExit(f"Naming rules not found: {mdc}")
    text = mdc.read_text(encoding="utf-8")
    required = [
        "Person_Type_Date_Detail",
        "Person_DocumentType_EXP_YYYY-MM-DD",
        "EXP_5555",
        "No EXP token for docs that don't expire",
        "Person_Carrier_Side",
        "Person_PhotoType_NN",
        "Project_Type_Date_Detail",
        "YYYY-MM-DD_Source_DocType_Detail",
        "YYYY_Tax_Form_Source",
        "Topic_Type_Detail",
        "lowercase extensions",
    ]
    missing = [item for item in required if item not in text]
    if missing:
        raise SystemExit("AGENTS.md is missing naming templates: " + "; ".join(missing))
    if "README.md" not in text:
        raise SystemExit("AGENTS.md no longer points at the README naming standard")
    dated = False
    if readme.is_file():
        dated = "Person_Photo_YYYY-MM-DD_Detail" in readme.read_text(encoding="utf-8")
    DATED_PHOTOS = dated
    RULES_READY = True
    RULES_LABEL = f"{mdc}" + ("; dated photos from README" if dated else "; dated photos off")


def id_conforms(name: str) -> bool:
    if re.search(r"(?i)(?:Exp-|EXP_)", name):
        return RX_ID_EXP.match(name) is not None
    return RX_ID_OPEN.match(name) is not None


def assert_naming_examples() -> None:
    cases = [
        ("Kid1_Dental_2026-10-01_Checkup.pdf", "person", True),
        ("You_DL_EXP_5555.jpg", "id", True),
        ("You_DL_Exp-5555.jpg", "id", True),
        ("Kid2_Passport_EXP_2031-03-18.pdf", "id", True),
        ("Kid2_Passport_Issued-2026-03-19_Exp-2031-03-18.pdf", "id", True),
        ("Kid1_BirthCertificate.pdf", "id", True),
        ("Kid1_BirthCertificate_2016-04-19_Amended.pdf", "id", True),
        ("You_DL_2_EXP_5555.pdf", "id", True),
        ("You_DL_2_Exp-5555.pdf", "id", True),
        ("Kid1_Insurer_Front.png", "insurance", True),
        ("Life Insurance C4.jpg", "insurance", False),
        ("Kid2_PassportPhoto_01.jpg", "photo", True),
        ("Sofa-Listing_Photo_01.png", "photo", True),
        ("Family_Photo_2025-07-04_Beach.jpg", "photo", DATED_PHOTOS),
        ("KitchenRemodel_Quote_2026-03-04_Contractor-A.pdf", "home", True),
        ("You_Citation_2026-07-11_Parking.pdf", "home", True),
        ("2026-02-21_Bank_Statement.pdf", "money", True),
        ("2026-02-21_HSA_Receipt_Rx-23-40.pdf", "money", True),
        ("Budget_Template_Annual.xlsx", "money", True),
        ("2025_Brokerage_Terms_IRA-Rollover-Promo.pdf", "money", False),
        ("2025_Tax_W2_Employer.pdf", "tax", True),
        ("2025_Tax_1098_Lender.pdf", "tax", True),
        ("2025_Tax_Checklist.csv", "tax", False),
        ("You_Resume_2025-11-10.pdf", "work", True),
        ("Employer_OfferLetter_2025-01-15.pdf", "work", True),
        ("Investing_Notes_Basics.pdf", "topic", True),
        ("Research_OnePager_Draft-Ideas.jpeg", "topic", True),
        ("Kid2_School_StudentCouncil-Poster.jpeg", "person", True),
        ("Kid1_School_2026-01-15_CourseSelection.jpeg", "person", True),
        ("scan.pdf", "person", False),
    ]
    bad = [f"{name} kind={kind} expected={ok}" for name, kind, ok in cases if name_conforms(name, kind) != ok]
    if bad:
        raise SystemExit("naming examples failed:\n" + "\n".join(bad))


def folder_kind(rel: str) -> str:
    top = rel.split("/", 1)[0]
    low = rel.lower()
    if re.search(r"(^|/)job search(/|$)", low):
        return "job-search"
    if top == "00 Inbox":
        return "inbox"
    if top == "40 Money":
        if re.search(r"(^|/)taxes(/|$)", low):
            return "tax"
        return "money"
    if top == "30 Home":
        return "home"
    if top == "10 Identity":
        if re.search(r"(^|/)insurance( cards)?(/|$)", low):
            return "insurance"
        if re.search(r"(^|/)(ids|passports)(/|$)", low):
            return "id"
        return "person"
    if top == "80 Photos":
        return "photo"
    if top in {"15 Travel", "20 Health", "50 Legal", "70 Kids", YOU_FOLDER}:
        return "person"
    if top == "60 Work":
        return "work"
    return "topic"


def name_conforms(name: str, kind: str) -> bool:
    if kind == "money":
        return RX_MONEY.match(name) is not None or RX_TOPIC.match(name) is not None
    if kind == "tax":
        return RX_TAX.match(name) is not None
    if kind == "id":
        return id_conforms(name) or RX_PHOTO.match(name) is not None
    if kind == "insurance":
        return RX_INSURANCE.match(name) is not None
    if kind == "photo":
        return RX_PHOTO.match(name) is not None or (DATED_PHOTOS and RX_PHOTO_DATED.match(name) is not None)
    if kind == "home":
        return RX_HOME.match(name) is not None or RX_PERSON.match(name) is not None
    if kind == "work":
        return RX_WORK.match(name) is not None
    if kind == "person":
        return RX_PERSON.match(name) is not None
    if kind in {"job-search", "inbox"}:
        return True
    return RX_TOPIC.match(name) is not None


def miscategorized(name: str, kind: str, rel: str) -> str | None:
    if kind not in {"money", "tax", "job-search"}:
        if (
            re.match(r"^(?:\d{4}-\d{2}-\d{2}|\d{4}_Tax)_", name) or re.search(r"(?i)(1098|1099-G|1099)", name)
        ):
            return "looks-like-money"
    lead = name.split("_", 1)[0]
    if kind in {"money", "tax"} and lead in PEOPLE and not re.match(r"^\d{4}", name):
        return "person-lead-in-money"
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    for who in sorted(PEOPLE):
        if re.search(rf"(?i)(^|[/ _]){re.escape(who)}($|[/ _])", parent):
            lead = name.split("_", 1)[0]
            if lead in PEOPLE and lead != who:
                return "person-mismatch"
            break
    return None


def under_inbox(rel: str) -> bool:
    return rel == "00 Inbox" or rel.startswith("00 Inbox/")


def norm_stem(name: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(name)
    stem_l = stem.lower()
    stem_l = re.sub(r"\s*\(\s*\d+\s*\)$", "", stem_l)
    stem_l = re.sub(r"(?i)[\s_-]*(copy|final|old)$", "", stem_l)
    stem_l = re.sub(r"[_-][bcd]$", "", stem_l)
    stem_l = re.sub(r"[\s_]+", "", stem_l)
    return stem_l, ext.lower()


def parse_expiry(name: str) -> tuple[str, date | None, str | None]:
    matches = RX_EXP_DATE.findall(name)
    if not matches:
        return "", None, None
    token = matches[-1]
    try:
        return token, date.fromisoformat(token), None
    except ValueError:
        return token, None, "invalid"


def audit_rows(conn: sqlite3.Connection, root_name: str, folder: str | None) -> list[sqlite3.Row]:
    if folder:
        prefix = folder.replace("\\", "/").strip("/")
        return conn.execute(
            """SELECT * FROM files
               WHERE root=? AND (rel_path=? OR rel_path LIKE ? ESCAPE '\\')
               ORDER BY rel_path""",
            (root_name, prefix, like_prefix(prefix)),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM files WHERE root=? ORDER BY rel_path",
        (root_name,),
    ).fetchall()


def live_rows(conn: sqlite3.Connection, root_cfg: dict, folder: str | None) -> tuple[list[dict], list[str]]:
    root = Path(root_cfg["path"])
    name = root_cfg["name"]
    errors: list[str] = []
    if folder:
        prefixes: list[str | None] = [folder.replace("\\", "/").strip("/")]
    else:
        prefixes = [p for p in (prefixes_for(root_cfg, None) or [None]) if p is None or (root / p).is_dir()]
    rows: list[dict] = []
    for prefix in prefixes:
        for rel, full, st, _protected in walk_prefix(root, prefix, errors, root_cfg):
            db = conn.execute(
                "SELECT sha256, size, mtime_ns FROM files WHERE root=? AND rel_path=?",
                (name, rel),
            ).fetchone()
            sha = None
            if db and db["size"] == st.st_size and db["mtime_ns"] == st.st_mtime_ns:
                sha = db["sha256"]
            rows.append(
                {
                    "rel_path": rel,
                    "name": full.name,
                    "ext": full.suffix.lower(),
                    "size": st.st_size,
                    "sha256": sha,
                }
            )
    return rows, errors


def cmd_audit(cfg: dict, args: argparse.Namespace) -> int:
    mdc, readme = naming_paths(cfg)
    load_naming_rules(mdc, readme)
    assert_naming_examples()
    exc = Exceptions()
    exc.load(cfg.get("exceptions"))
    today = now_et().date()
    cutoff = today + timedelta(days=args.days)
    conn = connect(cfg)
    lines: list[str] = []
    summaries: list[dict] = []
    exit_code = 0
    try:
        for root_cfg in selected_roots(cfg, args.root):
            rows, errors = live_rows(conn, root_cfg, args.folder)
            if errors:
                exit_code = 1
            groups = group_audit_rows(rows, args.folder)
            lines.append(f"AUDIT  {root_cfg['name']}  {root_cfg['path']}")
            lines.append(f"rules: {RULES_LABEL}")
            lines.append(f"exceptions: {exc.path or '(none)'}  loaded={exc.loaded}")
            if errors:
                lines.append(f"walk errors: {len(errors)}")
                for line in errors[:10]:
                    lines.append(f"    ERROR  {line}")
            for scope, group in groups:
                report, counts = audit_root(scope, group, exc, today, cutoff, args.days)
                lines.extend(report)
                summaries.append(counts)
            if not args.folder and len(groups) > 1:
                lines.append(total_line(summaries))
    finally:
        conn.close()
    text = "\n".join(lines)
    print(text)
    if not args.folder:
        report = getattr(args, "report", None)
        out = Path(report) if report else PROJECT / "plans" / "index-audit.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        header = f"Generated {now_et().isoformat(timespec='seconds')}\n\n"
        out.write_text(header + text + "\n", encoding="utf-8")
        print(f"\nfull report: {out}")
    if args.json:
        print("---json---")
        print(json.dumps(summaries, ensure_ascii=False))
    return exit_code


def group_audit_rows(rows: list[dict], folder: str | None) -> list[tuple[str, list[dict]]]:
    if folder:
        return [(folder.replace("\\", "/").strip("/"), rows)]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        top = row["rel_path"].split("/", 1)[0]
        groups.setdefault(top, []).append(row)
    return [(name, groups[name]) for name in sorted(groups)]


def total_line(summaries: list[dict]) -> str:
    files = sum(item["files"] for item in summaries)
    bad = sum(item["nonconforming"] for item in summaries)
    copies = sum(item["copy_pattern"] for item in summaries)
    exp = sum(item["exp_5555"] for item in summaries)
    misc = sum(item["miscategorized"] for item in summaries)
    return (
        f"TOTAL  files={files}  nonconforming={bad}  copy-pattern={copies}  "
        f"Exp-5555={exp}  miscategorized={misc}"
    )


def audit_root(scope: str, rows: list[dict], exc: Exceptions, today: date, cutoff: date, days: int):
    naming: list[str] = []
    copies: list[str] = []
    vague: list[str] = []
    version: list[str] = []
    exp5555: list[str] = []
    expiries: list[str] = []
    misc: list[str] = []
    skipped_exception = 0
    skipped_code = 0
    skipped_inbox = 0
    for row in rows:
        rel = row["rel_path"]
        name = row["name"]
        kind = folder_kind(rel)
        excepted = is_excepted(exc, rel, name) or kind == "job-search"
        code = exc.kind_code and is_code(name)
        inbox = under_inbox(rel)
        if excepted:
            skipped_exception += 1
        elif code:
            skipped_code += 1
        elif inbox:
            skipped_inbox += 1
        else:
            if not name_conforms(name, kind):
                naming.append(rel)
            stem = Path(name).stem
            if RX_VAGUE.match(stem) or RX_CAMERA.match(stem):
                vague.append(rel)
            if RX_COPY.search(name):
                copies.append(rel)
            if RX_VERSION.search(name):
                version.append(rel)
            why = miscategorized(name, kind, rel)
            if why:
                misc.append(f"{rel}  [{why}]")
        if not excepted and not code and RX_EXP5555.search(name):
            exp5555.append(rel)
        token, exp_date, invalid = parse_expiry(name)
        if token:
            if invalid or exp_date is None:
                expiries.append(f"{rel}  exp={token}  status=invalid")
            elif exp_date <= cutoff:
                status = "expired" if exp_date < today else "due"
                expiries.append(f"{rel}  exp={exp_date.isoformat()}  status={status}")

    exact, possible = duplicate_groups(rows)
    counts = {
        "folder": scope,
        "files": len(rows),
        "nonconforming": len(naming),
        "copy_pattern": len(copies),
        "exp_5555": len(exp5555),
        "miscategorized": len(misc),
    }
    report = [
        f"CHECK  {scope}",
        f"files scanned: {len(rows)}   skipped: exceptions/job-search={skipped_exception}  code={skipped_code}  inbox={skipped_inbox}",
        f"today: {today.isoformat()}   expiry window: through {cutoff.isoformat()} ({days} days, includes already expired)",
        f"nonconforming names: {len(naming)}",
        f"copy-pattern names: {len(copies)}",
        f"Exp-5555: {len(exp5555)}",
        f"miscategorized: {len(misc)}",
        block("nonconforming names", naming),
        block("copy-pattern names", copies),
        block("vague names", vague),
        block("version clutter", version),
        block("Exp-5555", exp5555),
        block("expiries", expiries),
        block("miscategorized", misc),
        duplicate_block("exact duplicates", exact),
        duplicate_block("possible duplicates", possible),
    ]
    return report, counts


def args_folder_label(rows: list[sqlite3.Row], root_cfg: dict) -> str:
    include = root_cfg.get("include")
    if include and rows:
        tops = sorted({row["rel_path"].split("/", 1)[0] for row in rows})
        return ", ".join(tops)
    if include and not rows:
        return ", ".join(include) + " (no indexed files)"
    return "<indexed files in this root>"


def block(title: str, items: list[str], limit: int = 15) -> str:
    lines = [f"{title + ':':<22} {len(items)}"]
    for item in items[:limit]:
        lines.append(f"    {item}")
    if len(items) > limit:
        lines.append(f"    ... {len(items) - limit} more")
    if not items:
        lines.append("    (none)")
    return "\n".join(lines)


def duplicate_groups(rows: list[sqlite3.Row]) -> tuple[list[list[str]], list[list[str]]]:
    by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if row["sha256"]:
            by_hash[row["sha256"]].append(row)
    exact = []
    for group in by_hash.values():
        if len(group) > 1:
            exact.append(sorted(row["rel_path"] for row in group))
    exact.sort(key=lambda g: g[0])

    by_name: dict[tuple[str, str, int], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        stem, ext = norm_stem(row["name"])
        if len(stem) < 6:
            continue
        by_name[(stem, ext, row["size"])].append(row)
    possible = []
    for group in by_name.values():
        hashes = {row["sha256"] for row in group}
        if len(group) > 1 and len(hashes) > 1:
            possible.append(sorted(f"{row['rel_path']}  {row['sha256'][:12]}" for row in group))
    possible.sort(key=lambda g: g[0])
    return exact, possible


def duplicate_block(title: str, groups: list[list[str]], limit: int = 8) -> str:
    file_count = sum(len(group) for group in groups)
    lines = [f"{title + ':':<22} {len(groups)} groups, {file_count} files"]
    shown = 0
    for group in groups:
        if shown >= limit:
            break
        shown += 1
        lines.append(f"    group {shown}")
        for item in group:
            lines.append(f"        {item}")
    if len(groups) > limit:
        lines.append(f"    ... {len(groups) - limit} more groups")
    if not groups:
        lines.append("    (none)")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sonar indexer (read-only on your files)")
    parser.add_argument("--config", type=Path, default=None, help="settings JSON (default: config.json, else config.example.json)")
    parser.add_argument("--db", type=Path, default=None, help="override the index database path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="incrementally hash and index configured folders")
    p_index.add_argument("--root", help="config root name, or a filesystem path to index")
    p_index.add_argument("--folder", action="append", help="relative folder to index; repeatable")
    p_index.add_argument("--hash-only", action="store_true", help="store SHA-256 and skip text extraction")
    p_index.add_argument(
        "--size-prefilter",
        action="store_true",
        help="hash only files whose size is shared; unique sizes are recorded without a hash",
    )

    p_search = sub.add_parser("search", help="full-text search of names, paths, and content")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--folder", help="top-level Family folder, e.g. '40 Money'")
    p_search.add_argument("--person", help="person token from config, e.g. Kid1")
    p_search.add_argument("--type", dest="file_type", help="PDF, Image, Word, Excel, Text, Video, eBooks, or Other")
    p_search.add_argument("--from", dest="date_from", help="start date YYYY-MM-DD")
    p_search.add_argument("--to", dest="date_to", help="end date YYYY-MM-DD")

    p_audit = sub.add_parser("audit", help="naming, expiry, and duplicate report")
    p_audit.add_argument("--root")
    p_audit.add_argument("--folder")
    p_audit.add_argument("--days", type=int, default=180)
    p_audit.add_argument("--json", action="store_true")
    p_audit.add_argument("--report", type=Path, default=None, help="write the full audit here instead of plans/index-audit.txt")
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "db", None):
        cfg["index_db"] = str(args.db)
    if args.cmd == "index":
        return cmd_index(cfg, args)
    if args.cmd == "search":
        return cmd_search(cfg, args)
    if args.cmd == "audit":
        return cmd_audit(cfg, args)
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
