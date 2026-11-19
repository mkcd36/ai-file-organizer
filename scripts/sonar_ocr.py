#!/usr/bin/env python3
"""Classify Family PDFs for OCRmyPDF, then apply only with --yes.

plan   reads PDFs and writes a log. It does not copy or modify cabinet files.
apply  copies each scanned original to To Review\\OCR Originals, then runs
       OCRmyPDF --skip-text. Refuses to run unless --yes is passed.

A PDF is "has text" when every page has at least 25 alphanumeric characters.
Pages below that are treated as scans. Mixed documents are ocr-partial and
still go through OCRmyPDF with --skip-text so existing text is kept.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sonar_index  # noqa: E402

TEXT_CHARS = 25
ORIGINALS = Path(sonar_index.load_settings()["review_root"]) / "OCR Originals"
OCR_ARGS = [
    "--skip-text",
    "--rotate-pages",
    "--deskew",
    "--language",
    "eng",
    "--optimize",
    "0",
    "--output-type",
    "pdf",
]
SKIP_NAMES = sonar_index.SKIP_NAMES


def alnum_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def classify_pdf(path: Path) -> dict:
    try:
        reader = PdfReader(str(path), strict=False)
    except PdfReadError as exc:
        return {"action": "error", "pages": 0, "text_pages": 0, "image_pages": 0, "alnum_chars": 0, "note": str(exc)}
    except Exception as exc:  # damaged or unreadable PDF
        return {"action": "error", "pages": 0, "text_pages": 0, "image_pages": 0, "alnum_chars": 0, "note": str(exc)}
    if reader.is_encrypted:
        return {
            "action": "skip-encrypted",
            "pages": len(reader.pages),
            "text_pages": 0,
            "image_pages": 0,
            "alnum_chars": 0,
            "note": "encrypted",
        }
    text_pages = 0
    image_pages = 0
    total = 0
    try:
        pages = list(reader.pages)
    except Exception as exc:
        return {"action": "error", "pages": 0, "text_pages": 0, "image_pages": 0, "alnum_chars": 0, "note": str(exc)}
    for page in pages:
        try:
            count = alnum_count(page.extract_text() or "")
        except Exception as exc:
            return {
                "action": "error",
                "pages": len(pages),
                "text_pages": text_pages,
                "image_pages": image_pages,
                "alnum_chars": total,
                "note": str(exc),
            }
        total += count
        if count >= TEXT_CHARS:
            text_pages += 1
        else:
            image_pages += 1
    if not pages:
        action = "skip-empty"
    elif image_pages == 0:
        action = "skip-has-text"
    elif text_pages == 0:
        action = "ocr"
    else:
        action = "ocr-partial"
    return {
        "action": action,
        "pages": len(pages),
        "text_pages": text_pages,
        "image_pages": image_pages,
        "alnum_chars": total,
        "note": "",
    }


def iter_files(folder: Path):
    for dirpath, dirnames, filenames in __import__("os").walk(folder, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in {"_index", ".git"}]
        for name in filenames:
            if name.lower() in SKIP_NAMES or name.startswith("~$") or name.lower().endswith(".lnk"):
                yield Path(dirpath) / name, "skip-system"
                continue
            yield Path(dirpath) / name, None


def original_dest(folder_name: str, folder: Path, source: Path, originals: Path | None = None) -> Path:
    rel = source.relative_to(folder)
    return (originals or ORIGINALS) / folder_name / rel


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


def build_rows(folder_name: str, folder: Path, originals: Path | None = None) -> list[dict]:
    rows = []
    for path, forced in iter_files(folder):
        rel = path.relative_to(folder).as_posix()
        dest = ""
        if forced:
            info = {"action": forced, "pages": "", "text_pages": "", "image_pages": "", "alnum_chars": "", "note": ""}
        elif path.suffix.lower() != ".pdf":
            info = {"action": "skip-not-pdf", "pages": "", "text_pages": "", "image_pages": "", "alnum_chars": "", "note": ""}
        else:
            info = classify_pdf(path)
            if info["action"] in {"ocr", "ocr-partial"}:
                dest = str(original_dest(folder_name, folder, path, originals))
        rows.append(
            {
                "source": str(path),
                "rel_path": rel,
                "action": info["action"],
                "pages": info["pages"],
                "text_pages": info["text_pages"],
                "image_pages": info["image_pages"],
                "alnum_chars": info["alnum_chars"],
                "original_dest": dest,
                "note": info["note"],
            }
        )
    return rows


def write_log(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["source", "rel_path", "action", "pages", "text_pages", "image_pages", "alnum_chars", "original_dest", "note"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(folder_name: str, rows: list[dict], log: Path, originals: Path | None = None) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    print(f"OCR PLAN  {folder_name}")
    print(f"files logged: {len(rows)}")
    for key in ("ocr", "ocr-partial", "skip-has-text", "skip-not-pdf", "skip-encrypted", "skip-empty", "skip-system", "error"):
        if counts.get(key):
            print(f"    {key}: {counts[key]}")
    would = [row for row in rows if row["action"] in {"ocr", "ocr-partial"}]
    print(f"would OCR: {len(would)}")
    for row in would[:12]:
        print(f"    {row['action']}  p={row['pages']}  img={row['image_pages']}  {row['rel_path']}")
    if len(would) > 12:
        print(f"    ... {len(would) - 12} more")
    print("ocrmypdf args: " + " ".join(OCR_ARGS))
    print(f"originals would be copied to: {(originals or ORIGINALS) / folder_name}")
    print(f"log: {log}")


def cmd_plan(folder_name: str, folder: Path | None = None, originals: Path | None = None) -> int:
    if folder is None:
        cfg = sonar_index.load_config(None)
        folder = family_folder(cfg, folder_name)
    rows = build_rows(folder_name, folder, originals)
    log = log_path("dryrun", folder_name)
    write_log(log, rows)
    print_summary(folder_name, rows, log, originals)
    return 1 if any(row["action"] == "error" for row in rows) else 0


def cmd_apply(folder_name: str, folder: Path | None = None, originals: Path | None = None) -> int:
    if folder is None:
        cfg = sonar_index.load_config(None)
        folder = family_folder(cfg, folder_name)
    rows = build_rows(folder_name, folder, originals)
    log = log_path("apply", folder_name)
    results = []
    copied = ocr_ok = skipped = errors = 0
    for row in rows:
        action = row["action"]
        if action not in {"ocr", "ocr-partial"}:
            row["note"] = row["note"] or "not applied"
            results.append(row)
            skipped += 1
            continue
        source = Path(row["source"])
        dest = Path(row["original_dest"])
        if dest.exists():
            row["action"] = "skip-original-exists"
            row["note"] = "archive already exists; left the cabinet file untouched"
            results.append(row)
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, dest)
        except OSError as exc:
            row["action"] = "error"
            row["note"] = f"could not archive original: {exc}"
            results.append(row)
            errors += 1
            continue
        copied += 1
        tmp = source.with_suffix(source.suffix + ".ocr-tmp.pdf")
        cmd = ["ocrmypdf", *OCR_ARGS, str(source), str(tmp)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            row["action"] = "error"
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            row["note"] = f"ocrmypdf exit {proc.returncode}; original kept. " + (detail[-1] if detail else "")
            results.append(row)
            errors += 1
            continue
        tmp.replace(source)
        row["action"] = "ocr-applied"
        row["note"] = f"original copied to {dest}"
        results.append(row)
        ocr_ok += 1
    write_log(log, results)
    print(f"OCR APPLY  {folder_name}")
    print(f"copied={copied}  ocr_ok={ocr_ok}  skipped={skipped}  errors={errors}")
    print(f"log: {log}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    sonar_index._utf8_stdio()
    parser = argparse.ArgumentParser(description="Dry-run or apply OCRmyPDF on one Family folder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for subparser in (sub.add_parser("plan"),):
        subparser.add_argument("--folder", required=False)
        subparser.add_argument("--root", type=Path, default=None, help="cabinet root; with --folder omitted, each child folder is planned")
        subparser.add_argument("--originals", type=Path, default=None)
        subparser.add_argument("--exclude", action="append", default=[])
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--folder", required=False)
    p_apply.add_argument("--root", type=Path, default=None, help="cabinet root; OCR each child folder except excludes")
    p_apply.add_argument("--originals", type=Path, default=None)
    p_apply.add_argument("--exclude", action="append", default=[])
    p_apply.add_argument("--yes", action="store_true", help="required before any copy or OCR")
    args = parser.parse_args(argv)
    if args.cmd == "plan":
        return _run_tree(cmd_plan, args)
    if args.cmd == "apply":
        if not args.yes:
            raise SystemExit("apply refused: pass --yes after the dry-run has been approved")
        return _run_tree(cmd_apply, args)
    return 2


def _run_tree(fn, args) -> int:
    originals = args.originals
    if args.root:
        root = Path(args.root)
        if not root.is_dir():
            raise SystemExit(f"Root not found: {root}")
        excludes = set(args.exclude or [])
        code = 0
        children = [root / args.folder] if args.folder else sorted(p for p in root.iterdir() if p.is_dir())
        for child in children:
            if child.name in excludes or not child.is_dir():
                print(f"OCR SKIP  {child.name}")
                continue
            code = max(code, fn(child.name, child, originals))
        return code
    if not args.folder:
        raise SystemExit("pass --folder or --root")
    return fn(args.folder, None, originals)


if __name__ == "__main__":
    raise SystemExit(main())
