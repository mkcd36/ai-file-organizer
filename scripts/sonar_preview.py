#!/usr/bin/env python3
"""Read-only document previews for Sonar.

Renders into %TEMP%\\sonar-previews. Originals are never modified.
Heavy libraries are imported only when a preview needs them so the app
starts quickly. Missing libraries fall back to metadata.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _local_zone():
    try:
        from sonar_index import load_settings

        return ZoneInfo(load_settings().get("timezone") or "America/New_York")
    except Exception:
        return ZoneInfo("America/New_York")


def _gs_path() -> Path:
    try:
        from sonar_index import load_settings

        value = load_settings().get("ghostscript") or ""
    except Exception:
        value = ""
    return Path(value) if value else Path("gswin64c-not-configured")


ET = _local_zone()
MAX_BYTES = 200 * 1024 * 1024
TEXT_LINES = 60
TEXT_BYTES = 256 * 1024
CACHE_SIZE = 32
DPI = 100
MAX_IMAGE_EDGE = 1600
GS_PATH = _gs_path()  # optional "ghostscript" path in config; else gswin64c on PATH
TEMP_NAME = "sonar-previews"
FILE_PREFIX = "pv-"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff"}
TEXT_EXTS = {".txt", ".csv", ".md", ".json", ".srt", ".vtt", ".log"}
DOCX_EXTS = {".docx"}
XLSX_EXTS = {".xlsx", ".xlsm"}

_CACHE: OrderedDict[tuple, "PreviewResult"] = OrderedDict()
_ATEXIT_REGISTERED = False
_HEIF_TRIED = False


@dataclass
class PreviewResult:
    kind: str
    source: Path
    title: str
    details: str
    image_path: Path | None = None
    text: str = ""
    page: int = 0
    page_count: int = 1
    error: str = ""
    outputs: list[Path] = field(default_factory=list)


def temp_dir() -> Path:
    root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or Path.home() / "AppData" / "Local" / "Temp")
    path = root / TEMP_NAME
    path.mkdir(parents=True, exist_ok=True)
    _ensure_atexit()
    return path


def _ensure_atexit() -> None:
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(cleanup_temp)
        _ATEXIT_REGISTERED = True


def cleanup_temp() -> None:
    path = Path(os.environ.get("TEMP") or os.environ.get("TMP") or Path.home() / "AppData" / "Local" / "Temp") / TEMP_NAME
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.name.startswith(FILE_PREFIX):
            try:
                if child.is_file():
                    child.unlink()
            except OSError:
                pass
    try:
        if not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def file_details(path: Path, extra: str = "") -> str:
    try:
        st = path.stat()
        size = format_size(st.st_size)
        when = datetime.fromtimestamp(st.st_mtime, ET).strftime("%Y-%m-%d %I:%M %p")
        folder = str(path.parent)
        parts = [size, when, folder]
        if extra:
            parts.insert(0, extra)
        return "  ·  ".join(parts)
    except OSError as exc:
        return str(exc)


def _cache_key(path: Path, page: int) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), st.st_mtime_ns, st.st_size, page)


def _cache_get(key: tuple | None) -> PreviewResult | None:
    if key is None or key not in _CACHE:
        return None
    _CACHE.move_to_end(key)
    return _CACHE[key]


def _cache_put(key: tuple | None, result: PreviewResult) -> PreviewResult:
    if key is None:
        return result
    _CACHE[key] = result
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)
    return result


def _out_path(path: Path, page: int, suffix: str) -> Path:
    raw = f"{path.resolve()}|{path.stat().st_mtime_ns}|{path.stat().st_size}|{page}|{suffix}"
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem)[:40].strip("-") or "file"
    return temp_dir() / f"{FILE_PREFIX}{safe}-{digest}{suffix}"


def _meta_result(path: Path, kind: str = "meta", error: str = "", extra: str = "", text: str = "") -> PreviewResult:
    return PreviewResult(
        kind=kind if not error else "error",
        source=path,
        title=path.name,
        details=file_details(path, extra=extra),
        error=error,
        text=text,
    )


def preview(path: Path, page: int = 0) -> PreviewResult:
    """Build a preview for path. Never writes to the original file."""
    path = Path(path)
    if not path.exists():
        return PreviewResult(
            kind="error",
            source=path,
            title=path.name,
            details=str(path.parent),
            error="File not found.",
        )
    if not path.is_file():
        return _meta_result(path, error="Not a file.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _meta_result(path, error=str(exc))
    if size > MAX_BYTES:
        return _meta_result(
            path,
            extra="too large to preview",
            error=f"Larger than 200 MB ({format_size(size)}). Open the file to view it.",
        )

    key = _cache_key(path, page)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            result = _preview_pdf(path, page)
        elif ext in IMAGE_EXTS:
            result = _preview_image(path)
        elif ext in TEXT_EXTS:
            result = _preview_text(path)
        elif ext in DOCX_EXTS:
            result = _preview_docx(path)
        elif ext in XLSX_EXTS:
            result = _preview_xlsx(path)
        else:
            result = _meta_result(path, extra=ext.lstrip(".").upper() or "file")
    except Exception as exc:
        result = _meta_result(path, error=f"Preview failed: {exc}")
    return _cache_put(key, result)


def _import_pymupdf():
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        import fitz as pymupdf  # type: ignore

        return pymupdf


def _pdf_page_count(path: Path) -> int | None:
    try:
        mupdf = _import_pymupdf()
        doc = mupdf.open(path)
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        pass
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def _render_pdf_mupdf(path: Path, page: int, dest: Path) -> int:
    mupdf = _import_pymupdf()
    doc = mupdf.open(path)
    try:
        count = int(doc.page_count)
        if count < 1:
            raise RuntimeError("PDF has no pages")
        index = min(max(page, 0), count - 1)
        zoom = DPI / 72.0
        pix = doc.load_page(index).get_pixmap(matrix=mupdf.Matrix(zoom, zoom), alpha=False)
        pix.save(str(dest))
        return count
    finally:
        doc.close()


def _render_pdf_ghostscript(path: Path, page: int, dest: Path) -> int:
    gs = GS_PATH if GS_PATH.is_file() else shutil.which("gswin64c")
    if not gs:
        raise RuntimeError("PyMuPDF and Ghostscript are both unavailable")
    count = _pdf_page_count(path) or 1
    index = min(max(page, 0), count - 1)
    proc = subprocess.run(
        [
            str(gs),
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-dQUIET",
            "-sDEVICE=png16m",
            f"-r{DPI}",
            f"-dFirstPage={index + 1}",
            f"-dLastPage={index + 1}",
            f"-sOutputFile={dest}",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 or not dest.is_file():
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(err or "Ghostscript failed")
    return count


def _preview_pdf(path: Path, page: int) -> PreviewResult:
    dest = _out_path(path, page, ".png")
    count = 1
    error = ""
    if not dest.is_file():
        try:
            count = _render_pdf_mupdf(path, page, dest)
        except Exception as exc:
            try:
                count = _render_pdf_ghostscript(path, page, dest)
            except Exception as exc2:
                error = f"Could not render PDF ({exc}; {exc2})"
                return _meta_result(path, extra="PDF", error=error)
    else:
        count = _pdf_page_count(path) or 1
    index = min(max(page, 0), max(count, 1) - 1)
    extra = f"PDF  ·  page {index + 1} of {count}"
    return PreviewResult(
        kind="image",
        source=path,
        title=path.name,
        details=file_details(path, extra=extra),
        image_path=dest,
        page=index,
        page_count=count,
        outputs=[dest],
    )


def _register_heif() -> None:
    global _HEIF_TRIED
    if _HEIF_TRIED:
        return
    _HEIF_TRIED = True
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass


def _preview_image(path: Path) -> PreviewResult:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return _meta_result(path, extra="image", error="Pillow is not installed.")
    if path.suffix.lower() in {".heic", ".heif"}:
        _register_heif()
    try:
        with Image.open(path) as raw:
            image = ImageOps.exif_transpose(raw)
            if image is raw:
                image = raw.copy()
            else:
                image = image.copy()
    except Exception as exc:
        return _meta_result(path, extra="image", error=f"Could not open image: {exc}")
    if image.mode not in {"RGB", "RGBA", "L"}:
        image = image.convert("RGB")
    if max(image.size) > MAX_IMAGE_EDGE:
        image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    dest = _out_path(path, 0, ".png")
    if not dest.is_file():
        image.save(dest, "PNG")
    extra = f"Image  ·  {image.size[0]}×{image.size[1]}"
    return PreviewResult(
        kind="image",
        source=path,
        title=path.name,
        details=file_details(path, extra=extra),
        image_path=dest,
        outputs=[dest],
    )


def _preview_text(path: Path) -> PreviewResult:
    data = path.read_bytes()[:TEXT_BYTES]
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()[:TEXT_LINES]
    body = "\n".join(lines)
    if not body.strip():
        body = "(empty file)"
    dest = _out_path(path, 0, ".txt")
    if not dest.is_file():
        dest.write_text(body, encoding="utf-8")
    extra = f"Text  ·  first {min(len(text.splitlines()), TEXT_LINES)} lines"
    return PreviewResult(
        kind="text",
        source=path,
        title=path.name,
        details=file_details(path, extra=extra),
        text=body,
        outputs=[dest],
    )


def _preview_docx(path: Path) -> PreviewResult:
    try:
        from docx import Document
    except ImportError:
        return _meta_result(path, extra="Word document")
    try:
        doc = Document(str(path))
        paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        body = "\n\n".join(paras[:20]) or "(no paragraph text)"
    except Exception as exc:
        return _meta_result(path, extra="Word document", error=str(exc))
    dest = _out_path(path, 0, ".txt")
    if not dest.is_file():
        dest.write_text(body, encoding="utf-8")
    return PreviewResult(
        kind="text",
        source=path,
        title=path.name,
        details=file_details(path, extra="Word document"),
        text=body,
        outputs=[dest],
    )


def _preview_xlsx(path: Path) -> PreviewResult:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return _meta_result(path, extra="Excel workbook")
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
        try:
            sheet = wb.active
            rows: list[str] = []
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= 20:
                    break
                cells = ["" if c is None else str(c) for c in row]
                rows.append("\t".join(cells).rstrip())
            body = "\n".join(rows) or "(empty sheet)"
        finally:
            wb.close()
    except Exception as exc:
        return _meta_result(path, extra="Excel workbook", error=str(exc))
    dest = _out_path(path, 0, ".txt")
    if not dest.is_file():
        dest.write_text(body, encoding="utf-8")
    return PreviewResult(
        kind="text",
        source=path,
        title=path.name,
        details=file_details(path, extra="Excel workbook"),
        text=body,
        outputs=[dest],
    )


def write_headless_previews(files: dict[str, Path], out_dir: Path | None = None) -> list[dict]:
    """Render previews for the headless test. Originals are only read."""
    dest_dir = out_dir or temp_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict] = []
    for label, source in files.items():
        result = preview(source, page=0)
        written: list[str] = []
        if result.image_path and result.image_path.is_file():
            target = dest_dir / f"{FILE_PREFIX}{label}{result.image_path.suffix}"
            if target.resolve() != result.image_path.resolve():
                shutil.copy2(result.image_path, target)
            written.append(str(target if target.is_file() else result.image_path))
        if result.kind == "text":
            target = dest_dir / f"{FILE_PREFIX}{label}.txt"
            target.write_text(result.text, encoding="utf-8")
            written.append(str(target))
        reports.append(
            {
                "label": label,
                "source": str(source),
                "kind": result.kind,
                "error": result.error,
                "page_count": result.page_count,
                "details": result.details,
                "outputs": written,
                "exists": all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in written),
            }
        )
    return reports


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 1:
        print("usage: sonar_preview.py <file> [page]", file=sys.stderr)
        return 2
    result = preview(Path(args[0]), page=int(args[1]) if len(args) > 1 else 0)
    print(f"{result.kind}  {result.title}")
    print(result.details)
    if result.error:
        print("error:", result.error)
    if result.image_path:
        print("image:", result.image_path)
    if result.text:
        print(result.text[:1000])
    return 1 if result.kind == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
