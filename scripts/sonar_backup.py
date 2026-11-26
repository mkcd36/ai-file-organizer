#!/usr/bin/env python3
"""Copy new cabinet files to an external drive without overwriting or deleting.

Dry-run by default. Pass --apply to copy. Exact SHA-256 matches already
recorded in the destination index (anywhere on that drive) are skipped.
If the destination path exists with a different hash, the incoming file
goes to the conflicts folder instead of overwriting. Nothing on the
destination is deleted.

Paths come from config.json (cabinet_root as the source; optional
backup.dest_root, backup.index_db, backup.conflicts_root) or from
--source / --dest / --index / --conflicts.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_PROJECT = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from sonar_index import (  # noqa: E402
    SKIP_DIRS,
    SKIP_NAMES,
    expand_path,
    load_settings,
    settings_path,
    sha256_file,
)


def python_exe() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def skip_name(name: str) -> bool:
    low = name.lower()
    return low in SKIP_NAMES or low.endswith(".lnk") or name.startswith("~$")


def drive_ready(path: Path) -> bool:
    drive = path.drive
    if not drive:
        return True
    return Path(drive + "\\").exists()


def load_known_hashes(index_db: Path) -> set[str]:
    if not index_db.is_file():
        raise FileNotFoundError(
            f"Destination index not found: {index_db}\n"
            "Plug in the drive and make sure its index exists (or build one with sonar_index.py)."
        )
    conn = sqlite3.connect(index_db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT sha256 FROM files WHERE sha256 IS NOT NULL AND sha256 != ''"
        )
        return {(row[0] or "").lower() for row in rows if row[0]}
    finally:
        conn.close()


def iter_source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        ]
        for name in filenames:
            if skip_name(name):
                continue
            yield Path(dirpath) / name


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1



def existing_conflict_with_hash(base: Path, digest: str) -> Path | None:
    """Return a conflict-folder file at this relative path (or -2, -3, …) with the same hash."""
    parent = base.parent
    candidates: list[Path] = []
    if base.exists():
        candidates.append(base)
    if parent.is_dir():
        stem, suffix = base.stem, base.suffix
        for item in parent.iterdir():
            if item == base or not item.is_file():
                continue
            if item.suffix != suffix:
                continue
            rest = item.stem[len(stem):] if item.stem.startswith(stem) else ""
            if rest.startswith("-") and rest[1:].isdigit():
                candidates.append(item)
    for path in candidates:
        try:
            if sha256_file(path).lower() == digest:
                return path
        except OSError:
            continue
    return None


def copy_new(src: Path, dest: Path) -> None:
    if dest.exists():
        raise FileExistsError(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if not dest.exists():
        raise OSError(f"copy did not create {dest}")


def default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = _PROJECT / "plans"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"backup-{stamp}.csv"


def run_reindex(dest_root: Path, index_db: Path, config: Path | None) -> tuple[int, str]:
    script = _SCRIPTS / "sonar_index.py"
    cmd = [python_exe(), str(script)]
    if config:
        cmd.extend(["--config", str(config)])
    cmd.extend(["--db", str(index_db), "index", "--root", str(dest_root)])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
    return proc.returncode, output


def write_row(writer, action: str, src, dest, digest: str, reason: str) -> None:
    writer.writerow(
        {
            "Time": datetime.now().isoformat(timespec="seconds"),
            "Action": action,
            "Source": str(src),
            "Dest": str(dest),
            "Sha256": digest,
            "Reason": reason,
        }
    )


def classify(src: Path, dest: Path, digest: str, known: set[str], conflicts_root: Path, source: Path):
    """Return (action_kind, target, reason) where action_kind is skip|copy|conflict|error."""
    if digest in known:
        return "skip", dest, "exact duplicate already on the destination (index hash)"
    if dest.exists():
        try:
            dest_digest = sha256_file(dest).lower()
        except OSError as exc:
            return "error", dest, f"could not hash existing destination: {exc}"
        if dest_digest == digest:
            known.add(digest)
            return "skip", dest, "destination already has the same file"
        rel = src.relative_to(source)
        conflict_base = conflicts_root / rel
        already = existing_conflict_with_hash(conflict_base, digest)
        if already is not None:
            known.add(digest)
            return "skip", already, "already saved under Sync Conflicts"
        return "conflict", unique_path(conflict_base), "same path, different hash; not overwritten"
    return "copy", dest, "new file"


def resolve_paths(args) -> tuple[Path, Path, Path, Path, Path | None]:
    """Source, dest, index, conflicts, and the config file used (if any)."""
    cfg_path = Path(args.config) if args.config else settings_path(None)
    settings: dict = {}
    used_config: Path | None = None
    if cfg_path.is_file():
        settings = load_settings(cfg_path)
        used_config = Path(settings.get("_config_path") or cfg_path)
    elif args.config:
        raise SystemExit(f"Config not found: {cfg_path}")

    backup = settings.get("backup") or {}
    source = args.source
    if source is None:
        cabinet = settings.get("cabinet_root")
        if not cabinet:
            raise SystemExit("No --source given and cabinet_root is not set in config.json.")
        source = Path(cabinet)
    else:
        source = Path(source)

    dest = args.dest
    if dest is None:
        raw = backup.get("dest_root")
        if not raw:
            raise SystemExit(
                "No destination given. Pass --dest or add a backup.dest_root entry in config.json "
                "(see _backup_example in config.example.json)."
            )
        dest = expand_path(raw)
    else:
        dest = Path(dest)

    index_db = args.index
    if index_db is None:
        raw = backup.get("index_db")
        if raw:
            index_db = expand_path(raw)
        else:
            # Common layout: <drive>\_index\index.db next to the dest cabinet.
            index_db = dest.parent / "_index" / "index.db"
    else:
        index_db = Path(index_db)

    conflicts = args.conflicts
    if conflicts is None:
        raw = backup.get("conflicts_root")
        if raw:
            conflicts = expand_path(raw)
        elif settings.get("review_root"):
            conflicts = Path(settings["review_root"]) / "Sync Conflicts"
        else:
            conflicts = dest.parent / "To Review" / "Sync Conflicts"
    else:
        conflicts = Path(conflicts)

    return source, dest, index_db, conflicts, used_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy new cabinet files to an external drive without overwriting or deleting (dry-run default)."
    )
    parser.add_argument("--apply", action="store_true", help="copy files; without this flag, only plan")
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="refresh the destination index after a successful apply",
    )
    parser.add_argument("--config", type=Path, default=None, help="config.json (default: SONAR_CONFIG or project config.json)")
    parser.add_argument("--log", type=Path, default=None, help="CSV log path (default: plans\\backup-<timestamp>.csv)")
    parser.add_argument("--source", type=Path, default=None, help="source cabinet (default: cabinet_root from config)")
    parser.add_argument("--dest", type=Path, default=None, help="destination cabinet (default: backup.dest_root from config)")
    parser.add_argument("--index", type=Path, default=None, help="destination index used for hash lookup")
    parser.add_argument("--conflicts", type=Path, default=None, help="folder for same-path hash conflicts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source, dest_root, index_db, conflicts_root, used_config = resolve_paths(args)
    apply = bool(args.apply)
    log_path = args.log or default_log_path()

    if not source.is_dir():
        print(f"Source cabinet not found: {source}", file=sys.stderr)
        return 1
    if not drive_ready(dest_root):
        drive = dest_root.drive or dest_root
        print(f"Destination drive {drive} is not connected. Plug it in and try again.", file=sys.stderr)
        return 1

    try:
        known = load_known_hashes(index_db)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"{mode}  source={source}")
    print(f"      dest={dest_root}")
    print(f"      index={index_db}  known_hashes={len(known):,}")
    print(f"      log={log_path}")

    counts = {"copy": 0, "skip": 0, "conflict": 0, "error": 0, "scanned": 0}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["Time", "Action", "Source", "Dest", "Sha256", "Reason"],
        )
        writer.writeheader()

        for src in iter_source_files(source):
            counts["scanned"] += 1
            dest = dest_root / src.relative_to(source)
            try:
                digest = sha256_file(src).lower()
            except OSError as exc:
                counts["error"] += 1
                write_row(writer, "ERROR", src, dest, "", f"could not hash source: {exc}")
                continue

            kind, target, reason = classify(src, dest, digest, known, conflicts_root, source)
            if kind == "error":
                counts["error"] += 1
                write_row(writer, "ERROR", src, target, digest, reason)
                continue
            if kind == "skip":
                counts["skip"] += 1
                write_row(writer, "SKIP", src, target, digest, reason)
                continue

            if apply:
                try:
                    copy_new(src, target)
                    known.add(digest)
                    action = "COPY" if kind == "copy" else "CONFLICT"
                    counts[kind] += 1
                    write_row(writer, action, src, target, digest, reason)
                except FileExistsError:
                    counts["error"] += 1
                    write_row(writer, "ERROR", src, target, digest, f"refused to overwrite {target}")
                except OSError as exc:
                    counts["error"] += 1
                    write_row(writer, "ERROR", src, target, digest, f"copy failed: {exc}")
            else:
                counts[kind] += 1
                action = "WOULD_COPY" if kind == "copy" else "WOULD_CONFLICT"
                write_row(writer, action, src, target, digest, reason)

    verb_copy = "copied" if apply else "would copy"
    verb_conflict = "sent to conflicts" if apply else "would send to conflicts"
    print(
        f"scanned={counts['scanned']}  {verb_copy}={counts['copy']}  "
        f"skipped={counts['skip']}  {verb_conflict}={counts['conflict']}  errors={counts['error']}"
    )
    print(f"log: {log_path}")
    if not apply:
        print("Dry-run only. Re-run with --apply to copy.")

    if args.reindex:
        if not apply:
            print("--reindex skipped (dry-run). Apply first, then reindex.", file=sys.stderr)
        elif counts["error"] and not (counts["copy"] or counts["conflict"]):
            print("--reindex skipped because the apply had errors and copied nothing.", file=sys.stderr)
        else:
            print(f"Reindexing {dest_root} into {index_db} …")
            code, output = run_reindex(dest_root, index_db, used_config)
            if output:
                print(output)
            if code != 0:
                print(f"Reindex failed with exit code {code}", file=sys.stderr)
                return code
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
