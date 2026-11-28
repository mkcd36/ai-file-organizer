#!/usr/bin/env python3
"""Read page 1 of scanned PDFs with OCRia Nano and store the text in the index.

plan   classifies PDFs and writes a log. It does not modify cabinet files.
apply  runs LiteParse (`lit parse --format text --target-pages 1
       --ocr-backend ocria-nano`) on scans whose first page has no text layer,
       then writes that text onto the index row. The PDF is left as it is.
       Refuses to run unless --yes is passed.

A page "has text" when it has at least 25 alphanumeric characters. Digital
PDFs are skipped. Books, photos, Job search, program files, driver's licenses,
x-ray pages, and secret-looking names are skipped. Handwriting stays a human
naming job: this reader is for typed scans.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sonar_index  # noqa: E402

TEXT_CHARS = 25
SKIP_NAMES = sonar_index.SKIP_NAMES
DEFAULT_EXCLUDES = ("65 Learning", "80 Photos", "60 Work")
AREA_MARKERS = (
    "65 learning",
    "80 photos",
    "job search",
    "program-data",
    "program data",
)
SECRET_NAME = re.compile(r"(?i)(serial|recovery-code|password|passwd|credential)")
WEAK_NAME = re.compile(
    r"(?i)(x-?rays?|driver'?s?[\s_-]*licen|(^|[_\s.-])dl([_\s.-]|$))"
)
PAGE_HEADER = re.compile(r"^--- Page \d+ ---\s*")


def alnum_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def find_lit() -> Path:
    found = shutil.which("lit")
    if found:
        return Path(found)
    candidate = Path(sys.executable).resolve().parent / "Scripts" / "lit.exe"
    if candidate.is_file():
        return candidate
    raise SystemExit(
        "lit is not installed. Run `python -m pip install liteparse`, then `lit models pull ocria-nano`. "
        "On Windows, lit.exe is in the Python Scripts folder."
    )


def skip_kind(folder_name: str, rel: str, name: str) -> str | None:
    blob = f"{folder_name}/{rel}".replace("\\", "/").lower()
    if any(marker in blob for marker in AREA_MARKERS):
        return "skip-area"
    if SECRET_NAME.search(name):
        return "skip-secret"
    if WEAK_NAME.search(name) or WEAK_NAME.search(rel):
        return "skip-weak"
    return None


def classify_pdf(path: Path) -> dict:
    try:
        reader = PdfReader(str(path), strict=False)
    except PdfReadError as exc:
        return _class("error", 0, 0, 0, 0, str(exc))
    except Exception as exc:
        return _class("error", 0, 0, 0, 0, str(exc))
    if reader.is_encrypted:
        try:
            pages = len(reader.pages)
        except Exception:
            pages = 0
        return _class("skip-encrypted", pages, 0, 0, 0, "encrypted")
    try:
        pages = list(reader.pages)
    except Exception as exc:
        return _class("error", 0, 0, 0, 0, str(exc))
    if not pages:
        return _class("skip-empty", 0, 0, 0, 0, "")
    text_pages = 0
    image_pages = 0
    total = 0
    page1 = 0
    for index, page in enumerate(pages):
        try:
            count = alnum_count(page.extract_text() or "")
        except Exception as exc:
            return _class("error", len(pages), text_pages, image_pages, total, str(exc))
        total += count
        if index == 0:
            page1 = count
        if count >= TEXT_CHARS:
            text_pages += 1
        else:
            image_pages += 1
    if page1 >= TEXT_CHARS:
        action = "skip-has-text"
    else:
        action = "read"
    return _class(action, len(pages), text_pages, image_pages, total, "")


def _class(action: str, pages: int, text_pages: int, image_pages: int, alnum_chars: int, note: str) -> dict:
    return {
        "action": action,
        "pages": pages,
        "text_pages": text_pages,
        "image_pages": image_pages,
        "alnum_chars": alnum_chars,
        "note": note,
    }


def iter_files(folder: Path):
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in {"_index", ".git"}]
        for name in filenames:
            if name.lower() in SKIP_NAMES or name.startswith("~$") or name.lower().endswith(".lnk"):
                yield Path(dirpath) / name, "skip-system"
                continue
            yield Path(dirpath) / name, None


def slug(folder_name: str) -> str:
    cleaned = []
    for ch in folder_name.lower():
        cleaned.append(ch if ch.isalnum() else "-")
    return "".join(cleaned).strip("-")


def log_path(kind: str, folder_name: str) -> Path:
    plans = Path(__file__).resolve().parent.parent / "plans"
    return plans / f"ocr-{slug(folder_name)}-{kind}.csv"


def family_folder(cfg: dict, folder_name: str) -> Path:
    roots = [r for r in cfg["roots"] if r["name"] == "cabinet"]
    if not roots:
        raise SystemExit("config has no cabinet root")
    folder = Path(roots[0]["path"]) / folder_name
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")
    return folder


def build_rows(folder_name: str, folder: Path) -> list[dict]:
    rows = []
    for path, forced in iter_files(folder):
        rel = path.relative_to(folder).as_posix()
        if forced:
            info = _class(forced, "", "", "", "", "")
        elif path.suffix.lower() != ".pdf":
            info = _class("skip-not-pdf", "", "", "", "", "")
        else:
            kind = skip_kind(folder_name, rel, path.name)
            if kind:
                info = _class(kind, "", "", "", "", "")
            else:
                try:
                    info = classify_pdf(path)
                except Exception as exc:
                    info = _class("error", 0, 0, 0, 0, str(exc))
        rows.append(
            {
                "source": str(path),
                "rel_path": rel,
                "action": info["action"],
                "pages": info["pages"],
                "text_pages": info["text_pages"],
                "image_pages": info["image_pages"],
                "alnum_chars": info["alnum_chars"],
                "original_dest": "",
                "note": info["note"],
            }
        )
    return rows


def write_log(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "source",
        "rel_path",
        "action",
        "pages",
        "text_pages",
        "image_pages",
        "alnum_chars",
        "original_dest",
        "note",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(folder_name: str, rows: list[dict], log: Path) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    print(f"OCR PLAN  {folder_name}")
    print(f"files logged: {len(rows)}")
    for key in (
        "read",
        "skip-has-text",
        "skip-not-pdf",
        "skip-encrypted",
        "skip-empty",
        "skip-system",
        "skip-area",
        "skip-weak",
        "skip-secret",
        "error",
    ):
        if counts.get(key):
            print(f"    {key}: {counts[key]}")
    would = [row for row in rows if row["action"] == "read"]
    print(f"would read page 1 with ocria-nano: {len(would)}")
    for row in would[:12]:
        print(f"    p={row['pages']}  img={row['image_pages']}  {row['rel_path']}")
    if len(would) > 12:
        print(f"    ... {len(would) - 12} more")
    print("reader: lit parse --format text --target-pages 1 --ocr-backend ocria-nano")
    print("pdfs are not modified")
    print(f"log: {log}")


def read_page(source: Path, lit: Path) -> tuple[str, str]:
    handle = tempfile.NamedTemporaryFile(prefix="ocr-page-", suffix=".txt", delete=False)
    handle.close()
    dest = Path(handle.name)
    cmd = [
        str(lit),
        "parse",
        str(source),
        "--format",
        "text",
        "--target-pages",
        "1",
        "--ocr-backend",
        "ocria-nano",
        "--dpi",
        "250",
        "-q",
        "-o",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not dest.is_file():
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return "", f"lit exit {proc.returncode}. " + (detail[-1] if detail else "")
        text = PAGE_HEADER.sub("", dest.read_text(encoding="utf-8", errors="replace")).strip()
        return text, ""
    finally:
        dest.unlink(missing_ok=True)


def index_key(cfg: dict, source: Path) -> tuple[str, str] | None:
    resolved = source.resolve()
    for root in cfg["roots"]:
        root_path = Path(root["path"]).resolve()
        try:
            rel = resolved.relative_to(root_path).as_posix()
        except ValueError:
            continue
        return root["name"], rel
    return None


def open_index(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise SystemExit(f"Index not found: {db}. Build it before reading scans.")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def store_page_text(conn: sqlite3.Connection, root_name: str, rel_path: str, source: Path, text: str) -> str:
    row = conn.execute(
        "SELECT id FROM files WHERE root=? AND rel_path=?",
        (root_name, rel_path),
    ).fetchone()
    chars = len(text)
    indexed_at = sonar_index.now_et().isoformat(timespec="seconds")
    if row:
        conn.execute(
            """UPDATE files
               SET body=?, content_status=?, content_error=?, content_chars=?, indexed_at=?
               WHERE id=?""",
            (text, "ok", "", chars, indexed_at, row["id"]),
        )
        return "updated"
    stat = source.stat()
    conn.execute(
        """INSERT INTO files(
               root, rel_path, name, ext, size, mtime_ns, sha256, indexed_at,
               split_name, body, content_status, content_error, content_chars
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            root_name,
            rel_path,
            source.name,
            source.suffix.lower(),
            stat.st_size,
            stat.st_mtime_ns,
            sonar_index.sha256_file(source),
            indexed_at,
            sonar_index.split_filename(source.name),
            text,
            "ok",
            "",
            chars,
        ),
    )
    return "inserted"


def cmd_plan(folder_name: str, folder: Path | None = None, cfg: dict | None = None) -> int:
    if folder is None:
        cfg = cfg or sonar_index.load_config(None)
        folder = family_folder(cfg, folder_name)
    rows = build_rows(folder_name, folder)
    log = log_path("dryrun", folder_name)
    write_log(log, rows)
    print_summary(folder_name, rows, log)
    return 1 if any(row["action"] == "error" for row in rows) else 0


def cmd_apply(folder_name: str, folder: Path | None = None, cfg: dict | None = None) -> int:
    cfg = cfg or sonar_index.load_config(None)
    if folder is None:
        folder = family_folder(cfg, folder_name)
    lit = find_lit()
    rows = build_rows(folder_name, folder)
    log = log_path("apply", folder_name)
    conn = open_index(sonar_index.db_path(cfg))
    results = []
    read_ok = skipped = errors = unindexed = 0
    try:
        for row in rows:
            if row["action"] != "read":
                row["note"] = row["note"] or "not applied"
                results.append(row)
                skipped += 1
                continue
            source = Path(row["source"])
            text, err = read_page(source, lit)
            if err or alnum_count(text) < TEXT_CHARS:
                row["action"] = "error"
                row["note"] = err or "page 1 still has no usable text"
                results.append(row)
                errors += 1
                continue
            key = index_key(cfg, source)
            if key is None:
                row["action"] = "read-unindexed"
                row["note"] = "page was read, but this file is outside the configured cabinet root"
                results.append(row)
                unindexed += 1
                continue
            outcome = store_page_text(conn, key[0], key[1], source, text)
            row["action"] = "read-applied"
            row["note"] = f"ocria-nano page 1, {len(text)} chars, index {outcome}"
            results.append(row)
            read_ok += 1
        conn.commit()
    finally:
        conn.close()
    write_log(log, results)
    print(f"OCR APPLY  {folder_name}")
    print(f"read_ok={read_ok}  skipped={skipped}  unindexed={unindexed}  errors={errors}")
    print("pdfs were not modified")
    print(f"log: {log}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    sonar_index._utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Read page 1 of scanned PDFs with OCRia Nano into the local index"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        subparser = sub.add_parser(name)
        subparser.add_argument("--folder", required=False)
        subparser.add_argument("--root", type=Path, default=None, help="cabinet root; each child folder is visited")
        subparser.add_argument("--config", type=Path, default=None)
        subparser.add_argument("--db", type=Path, default=None, help="index database; overrides config index_db")
        subparser.add_argument("--exclude", action="append", default=[])
        if name == "apply":
            subparser.add_argument("--yes", action="store_true", help="required before any read is stored")
    args = parser.parse_args(argv)
    cfg = sonar_index.load_config(args.config)
    if args.db:
        cfg["index_db"] = str(args.db)
    if args.cmd == "plan":
        return _run_tree(cmd_plan, args, cfg)
    if args.cmd == "apply":
        if not args.yes:
            raise SystemExit("apply refused: pass --yes after the dry-run has been approved")
        return _run_tree(cmd_apply, args, cfg)
    return 2


def _run_tree(fn, args, cfg: dict) -> int:
    if args.root:
        root = Path(args.root)
        if not root.is_dir():
            raise SystemExit(f"Root not found: {root}")
        excludes = set(args.exclude or [])
        if not args.folder:
            excludes.update(DEFAULT_EXCLUDES)
        code = 0
        children = [root / args.folder] if args.folder else sorted(p for p in root.iterdir() if p.is_dir())
        for child in children:
            if child.name in excludes or not child.is_dir():
                print(f"OCR SKIP  {child.name}")
                continue
            code = max(code, fn(child.name, child, cfg))
        return code
    if not args.folder:
        raise SystemExit("pass --folder or --root")
    return fn(args.folder, None, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
