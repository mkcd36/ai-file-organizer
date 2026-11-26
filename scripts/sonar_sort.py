#!/usr/bin/env python3
"""ssort: preview and apply folder sorts into the Family cabinet.

Default is preview (writes a plan, moves nothing). Apply uses Move-Item
semantics: no overwrite, skip collisions, never delete. Undo moves files
back only when the dest still exists and the original source path is free.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from sonar_index import load_settings, now_et  # noqa: E402

PROJECT = _SCRIPTS.parent
DEFAULT_RULES = _SCRIPTS / "sonar_sort_rules.json"
PLANS_DIR = PROJECT / "plans"
SORT_PLANS_DIR = PLANS_DIR / "sort-plans"
SKIP_NAMES = {"desktop.ini", "thumbs.db"}
SKIP_EXTS = {".lnk"}
INCOMPLETE_EXTS = {".crdownload", ".tmp", ".partial", ".download"}
YEAR_RE = re.compile(r"(?<![0-9])(20\d{2})(?![0-9])")
DATE_RE = re.compile(r"(20\d{2})-\d{2}(?:-\d{2})?")

PROTECTED_PREFIXES = [
    Path(r"C:\Windows"),
    Path(r"C:\Program Files"),
    Path(r"C:\Program Files (x86)"),
    Path.home() / "AppData",
]


@dataclass(frozen=True)
class SortItem:
    source: Path
    dest: Path
    rule: str
    confidence: float
    action: str
    note: str = ""


@dataclass
class ApplyResult:
    moved: int = 0
    skipped: int = 0
    errors: int = 0
    lines: list[str] = field(default_factory=list)
    undo_path: Path | None = None


def stamp() -> str:
    return now_et().strftime("%Y%m%d-%H%M%S")


def load_rules(path: Path | None = None, config: Path | None = None) -> dict:
    """Rules JSON plus your settings (cabinet root, review root, people, children)."""
    rules_path = path or DEFAULT_RULES
    with rules_path.open(encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if "rules" not in data:
        raise ValueError(f"Rules file needs a rules list: {rules_path}")
    settings = load_settings(config)
    data.setdefault("cabinet_root", settings["cabinet_root"])
    data.setdefault("deletion_root", str(Path(settings["review_root"]) / "For Deletion"))
    data.setdefault("people", settings.get("people") or {})
    data.setdefault("children", settings.get("children") or [])
    data.setdefault("you", settings.get("you") or "You")
    data.setdefault("you_folder", settings.get("you_folder") or "75 You")
    data.setdefault("employers", settings.get("employers") or {})
    return data


def cabinet_root(rules: dict, dest_root: Path | None) -> Path:
    if dest_root is not None:
        return dest_root
    return Path(rules["cabinet_root"])


def deletion_root(rules: dict, dest_root: Path | None) -> Path:
    if dest_root is not None:
        return dest_root / "To Review" / "For Deletion"
    return Path(rules["deletion_root"])


def is_protected(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    try:
        # %TEMP% lives under AppData but is where test cabinets go.
        if resolved.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            return False
    except OSError:
        pass
    for prefix in PROTECTED_PREFIXES:
        try:
            base = prefix.resolve()
        except OSError:
            base = prefix
        if resolved == base or resolved.is_relative_to(base):
            return True
    return False


def pdf_first_page_text(path: Path, limit: int = 4000) -> str:
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        if not reader.pages:
            return ""
        text = reader.pages[0].extract_text() or ""
        return text[:limit]
    except Exception:
        return ""


def detect_people(text: str, rules: dict) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    people = rules.get("people") or {}
    for canonical, aliases in people.items():
        for alias in aliases:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", lowered):
                found.append(canonical)
                break
    return found


def detect_employers(text: str, rules: dict) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    employers = rules.get("employers") or {}
    for canonical, aliases in employers.items():
        names = aliases if isinstance(aliases, (list, tuple)) else [aliases]
        for alias in list(names) + [canonical]:
            if not str(alias).strip():
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(str(alias).lower())}(?![a-z0-9])", lowered):
                found.append(canonical)
                break
    return found


def detect_year(text: str) -> str | None:
    years = YEAR_RE.findall(text)
    return years[0] if years else None


def detect_photo_year(text: str) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    return f"{match.group(1)}_Photos"


def compile_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def haystacks(name: str, pdf_text: str) -> tuple[str, str]:
    filename = name.replace("_", " ").replace("-", " ")
    return name + " " + filename, (pdf_text or "")


def keyword_in(text: str, keyword: str) -> bool:
    pattern = rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None


def rule_hits(rule: dict, filename: str, filename_hay: str, pdf_text: str) -> tuple[bool, str]:
    match = rule.get("match") or {}
    mode = (rule.get("match_mode") or "any").lower()
    use_text = match.get("use_text", True)
    text = pdf_text if use_text else ""
    groups: list[tuple[str, bool]] = []

    extensions = [ext.lower() for ext in match.get("extensions") or []]
    if extensions:
        ext = Path(filename).suffix.lower()
        groups.append(("extensions", ext in extensions))

    pattern = match.get("filename_regex")
    regex_name = False
    regex_text = False
    if pattern:
        rx = compile_regex(pattern)
        regex_name = bool(rx.search(filename) or rx.search(filename_hay))
        regex_text = bool(text and rx.search(text))
        groups.append(("regex", regex_name or regex_text))

    keywords = [kw.lower() for kw in match.get("keywords") or []]
    name_hit = False
    text_hit = False
    if keywords:
        blob_name = filename + " " + filename_hay
        name_hit = any(keyword_in(blob_name, kw) for kw in keywords)
        text_hit = bool(text) and any(keyword_in(text, kw) for kw in keywords)
        groups.append(("keywords", name_hit or text_hit))

    if not groups:
        return False, "none"
    hits = [hit for _, hit in groups]
    ok = all(hits) if mode == "all" else any(hits)
    if not ok:
        return False, "none"
    filename_matched = regex_name or name_hit
    if extensions and Path(filename).suffix.lower() in extensions and not keywords and not pattern:
        filename_matched = True
    if filename_matched:
        return True, "filename"
    return True, "text" if text else "filename"


def resolve_dest_template(
    template: str,
    rules: dict,
    dest_root: Path | None,
    year: str | None,
    people: list[str],
    photo_year: str | None,
    employer: str | None = None,
) -> tuple[Path | None, str]:
    children = set(rules.get("children") or [])
    child = next((p for p in people if p in children), None)
    person = people[0] if people else None
    note = ""
    dest = template.replace("{you_folder}", rules.get("you_folder") or "75 You")
    dest = dest.replace("{you}", rules.get("you") or "You")
    if "{year}" in dest:
        if year:
            dest = dest.replace("{year}", year)
        else:
            dest = dest.replace("{year}_Tax", "").replace("{year}_Trips", "").replace("{year}", "")
            dest = re.sub(r"\\{2,}", r"\\", dest).strip("\\")
            note = "no year in name or text"
    if "{employer}" in dest:
        if not employer:
            return None, "no employer detected"
        dest = dest.replace("{employer}", employer)
    if "{photo_year}" in dest:
        if not photo_year:
            return None, "no photo date"
        dest = dest.replace("{photo_year}", photo_year)
    if "{child}" in dest:
        if not child:
            return None, "no child detected"
        dest = dest.replace("{child}", child)
    if "{person}" in dest:
        if not person:
            return None, "no person detected"
        dest = dest.replace("{person}", person)

    dest = dest.strip("\\")
    if dest.startswith("For Deletion\\") or dest == "For Deletion":
        rel = dest[len("For Deletion\\") :] if dest.startswith("For Deletion\\") else ""
        base = deletion_root(rules, dest_root)
        return (base / rel) if rel else base, note
    return cabinet_root(rules, dest_root) / dest, note


def classify_file(path: Path, rules: dict, dest_root: Path | None) -> SortItem:
    name = path.name
    low_name = name.lower()
    inbox_dir = cabinet_root(rules, dest_root) / (rules.get("inbox") or "00 Inbox")
    inbox_dest = inbox_dir / name

    if is_protected(path):
        return SortItem(path, path, "protected", 1.0, "skip", "protected path")
    if low_name in SKIP_NAMES or path.suffix.lower() in SKIP_EXTS or name.startswith("~$"):
        return SortItem(path, path, "skip_system", 1.0, "skip", "shortcut or system file")
    if path.suffix.lower() in INCOMPLETE_EXTS:
        return SortItem(path, path, "skip_incomplete", 1.0, "skip", "incomplete download")

    pdf_text = pdf_first_page_text(path)
    name_hay, _ = haystacks(name, "")
    people = detect_people(name + " " + name_hay + " " + pdf_text, rules)
    year = detect_year(name) or detect_year(pdf_text)
    photo_year = detect_photo_year(name)
    employers = detect_employers(name + " " + name_hay + " " + pdf_text, rules)
    employer = employers[0] if employers else None

    threshold = float(rules.get("low_confidence") or 0.6)
    text_scale = float(rules.get("text_only_scale") or 0.8)

    for rule in rules.get("rules") or []:
        hit, source = rule_hits(rule, name, name_hay, pdf_text)
        if not hit:
            continue
        requires = rule.get("requires")
        children = set(rules.get("children") or [])
        child = next((p for p in people if p in children), None)
        if requires == "child" and not child:
            continue
        if requires == "person" and not people:
            continue
        if requires == "employer" and not employer:
            continue
        dest, note = resolve_dest_template(
            rule.get("dest") or "",
            rules,
            dest_root,
            year,
            people,
            photo_year,
            employer,
        )
        if dest is None:
            continue
        confidence = float(rule.get("confidence") or 0.7)
        if source == "text":
            confidence *= text_scale
        if confidence < threshold:
            return SortItem(
                path,
                inbox_dest,
                f"low-confidence:{rule.get('id')}",
                round(confidence, 3),
                "inbox",
                note or "below confidence threshold",
            )
        return SortItem(
            path,
            dest / name,
            str(rule.get("id") or "rule"),
            round(confidence, 3),
            "move",
            note,
        )

    return SortItem(path, inbox_dest, "unmatched", 0.0, "inbox", "no rule matched")


def list_files(folder: Path, recursive: bool) -> list[Path]:
    if recursive:
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d not in {"_index", ".git", "__pycache__"}]
            for filename in filenames:
                files.append(Path(dirpath) / filename)
        return sorted(files)
    return sorted(p for p in folder.iterdir() if p.is_file())


def plan_path_for(folder: Path, when: str | None = None) -> Path:
    SORT_PLANS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", folder.name).strip("-") or "folder"
    return SORT_PLANS_DIR / f"sort-{safe}-{when or stamp()}.csv"


def write_plan(items: list[SortItem], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["source", "dest", "rule", "confidence", "action"])
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    "source": str(item.source),
                    "dest": str(item.dest),
                    "rule": item.rule,
                    "confidence": f"{item.confidence:.3f}",
                    "action": item.action,
                }
            )
    return path


def read_plan(path: Path) -> list[SortItem]:
    items: list[SortItem] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            items.append(
                SortItem(
                    source=Path(row["source"]),
                    dest=Path(row["dest"]),
                    rule=row.get("rule") or "",
                    confidence=float(row.get("confidence") or 0),
                    action=(row.get("action") or "").strip().lower(),
                )
            )
    return items


def preview_folder(
    folder: Path,
    rules: dict | None = None,
    recursive: bool = False,
    dest_root: Path | None = None,
    plan_out: Path | None = None,
) -> tuple[list[SortItem], Path]:
    folder = folder.expanduser()
    if not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")
    if is_protected(folder):
        raise PermissionError(f"Refusing to scan a protected folder: {folder}")
    rules = rules or load_rules()
    items = [classify_file(path, rules, dest_root) for path in list_files(folder, recursive)]
    out = plan_out or plan_path_for(folder)
    write_plan(items, out)
    return items, out


def action_counts(items: list[SortItem]) -> dict[str, int]:
    counts = {"move": 0, "inbox": 0, "skip": 0}
    for item in items:
        counts[item.action] = counts.get(item.action, 0) + 1
    return counts


def undo_log_path(when: str | None = None) -> Path:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    return PLANS_DIR / f"undo-log-sort-{when or stamp()}.csv"


def write_undo_log(rows: list[tuple[Path, Path, str]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Source", "Dest", "Time"])
        writer.writeheader()
        for source, dest, time in rows:
            writer.writerow({"Source": str(source), "Dest": str(dest), "Time": time})
    return path


def read_undo_log(path: Path) -> list[tuple[Path, Path]]:
    rows: list[tuple[Path, Path]] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            source = row.get("Source") or row.get("source")
            dest = row.get("Dest") or row.get("dest")
            if source and dest:
                rows.append((Path(source), Path(dest)))
    return rows


def latest_undo_log() -> Path | None:
    logs = sorted(PLANS_DIR.glob("undo-log-sort-*.csv"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def move_no_overwrite(source: Path, dest: Path) -> str | None:
    """Move source to dest. Return a skip/error reason, or None on success."""
    if not source.exists():
        return "source missing"
    if is_protected(source) or is_protected(dest):
        return "protected path"
    if dest.exists():
        return "destination exists"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
    except OSError as exc:
        return str(exc)
    return None


def apply_plan(plan: Path, items: list[SortItem] | None = None) -> ApplyResult:
    rows = items if items is not None else read_plan(plan)
    result = ApplyResult()
    undo_rows: list[tuple[Path, Path, str]] = []
    when = now_et().isoformat(timespec="seconds")
    for item in rows:
        if item.action == "skip":
            result.skipped += 1
            result.lines.append(f"SKIP    {item.source} ({item.rule})")
            continue
        if item.action not in {"move", "inbox"}:
            result.skipped += 1
            result.lines.append(f"SKIP    {item.source} (unknown action {item.action})")
            continue
        reason = move_no_overwrite(item.source, item.dest)
        if reason:
            result.skipped += 1
            result.lines.append(f"SKIP    {item.source} -> {item.dest} ({reason})")
            continue
        result.moved += 1
        undo_rows.append((item.source, item.dest, when))
        result.lines.append(f"MOVED   {item.source} -> {item.dest}")
    if undo_rows:
        result.undo_path = write_undo_log(undo_rows, undo_log_path())
    return result


def undo_moves(undo_path: Path) -> ApplyResult:
    result = ApplyResult()
    for source, dest in read_undo_log(undo_path):
        # Undo only when dest still exists and the original source path is free.
        if not dest.exists():
            result.skipped += 1
            result.lines.append(f"SKIP    {dest} (dest missing)")
            continue
        if source.exists():
            result.skipped += 1
            result.lines.append(f"SKIP    {dest} (source already exists)")
            continue
        reason = move_no_overwrite(dest, source)
        if reason:
            result.skipped += 1
            result.lines.append(f"SKIP    {dest} -> {source} ({reason})")
            continue
        result.moved += 1
        result.lines.append(f"RESTORED {dest} -> {source}")
    return result


def print_preview(items: list[SortItem], plan: Path) -> None:
    counts = action_counts(items)
    print(f"PLAN    {plan}")
    print(
        f"files={len(items)}  move={counts.get('move', 0)}  "
        f"inbox={counts.get('inbox', 0)}  skip={counts.get('skip', 0)}"
    )
    for item in items:
        print(
            f"  {item.action.upper():<6}  {item.confidence:.2f}  {item.rule:<28}  "
            f"{item.source.name} -> {item.dest}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or apply a Family cabinet folder sort.")
    parser.add_argument("folder", nargs="?", help="folder to scan (top-level files unless --recursive)")
    parser.add_argument("--apply", metavar="PLAN.CSV", help="apply a plan CSV (no overwrite, never delete)")
    parser.add_argument("--undo", metavar="UNDO.CSV", help="move files back from an undo log")
    parser.add_argument("--recursive", action="store_true", help="include files in subfolders")
    parser.add_argument("--dest-root", type=Path, help="override the cabinet root (for tests; nothing lands in your cabinet)")
    parser.add_argument("--rules", type=Path, default=None, help="rules JSON (default: scripts/sonar_sort_rules.json)")
    parser.add_argument("--config", type=Path, default=None, help="settings JSON (default: config.json, else config.example.json)")
    parser.add_argument("--plan-out", type=Path, default=None, help="write the preview plan here")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)

    if args.undo:
        undo_path = Path(args.undo)
        if not undo_path.is_file():
            print(f"Undo log not found: {undo_path}", file=sys.stderr)
            return 1
        result = undo_moves(undo_path)
        for line in result.lines:
            print(line)
        print(f"UNDO    restored={result.moved}  skipped={result.skipped}  errors={result.errors}")
        return 0 if result.errors == 0 else 1

    if args.apply:
        plan = Path(args.apply)
        if not plan.is_file():
            print(f"Plan not found: {plan}", file=sys.stderr)
            return 1
        result = apply_plan(plan)
        for line in result.lines:
            print(line)
        undo = f"  undo={result.undo_path}" if result.undo_path else ""
        print(f"APPLY   moved={result.moved}  skipped={result.skipped}  errors={result.errors}{undo}")
        return 0 if result.errors == 0 else 1

    if not args.folder:
        print("folder is required unless --apply or --undo is used", file=sys.stderr)
        return 2
    try:
        rules = load_rules(args.rules, args.config)
        items, plan = preview_folder(
            Path(args.folder),
            rules=rules,
            recursive=args.recursive,
            dest_root=args.dest_root,
            plan_out=args.plan_out,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print_preview(items, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
