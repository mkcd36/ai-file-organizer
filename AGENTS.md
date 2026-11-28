# AGENTS.md

Rules for any AI agent working in this project (Cursor, Codex, Claude Code, Grok, or similar). `CLAUDE.md` and `.cursor/rules/agents.mdc` point here.

## 0. Before anything

1. Read `README.md` in full. It is the source of truth for the folder structure (section 3), the naming convention (section 4), and the lessons learned (section 5). If this file and the README disagree, the README wins. Say so and propose a fix to this file.
2. Read `config.json` (or `config.example.json` if there's no `config.json`) for the cabinet root, review root, people tokens and folder list. Never hard-code a person's real name or a path in a script. Put it in `config.json`.
3. Follow the session protocol (section 4 below) for every folder you work on.

## 1. File moves (hard rules)

- **Never delete.** "Delete" always means *move to* `To Review\For Deletion\<reason>\`, keeping the relative path. The human empties it.
- **Never overwrite.** Check that the target is free before every move or rename. On a collision, skip the file, log it, and report it. Never use `Move-Item -Force`, `Copy-Item -Force` or `-Force` on anything that touches user files. No recursive deletes, ever.
- **Never move `.lnk` or `desktop.ini`** (also skip `thumbs.db`, `~$*` temp files, `System Volume Information`, `$RECYCLE.BIN`, `FOUND.000`).
- **Explicit paths.** Pass full, explicit paths and use `-LiteralPath` (brackets in names break wildcards). Don't add Desktop or Documents as workspace roots.
- **Dry run first.** Every move or rename is first written as a plan (a manifest CSV `Source,Dest,Group,Reason,DateSource`, or an `ssort` plan) and shown with counts, samples, held files and recommendations. Apply only after the human approves. Unattended runs (README section 10) apply with an undo log.
- **Undo log for every apply.** Every applied move writes `Source,Dest,Time` to `plans\undo-log-<yyyyMMdd-HHmmss>.csv` (add `-2`, `-3` on a timestamp collision, never overwrite another log). `scripts\undo-manifest.ps1 -Log <csv> -Apply` and `ssort --undo <csv>` reverse it.
- **Back up before editing.** Before you edit any script, rule file or README, copy it to `plans\` with a timestamp. Never leave `.bak` files beside the original.
- **Hash before dedupe.** Only exact SHA-256 matches are duplicates. The same name or size proves nothing. Never auto-thin drafts, near-copies or "looks similar" sets.
- **Don't restore what the human deleted.** Files in the Recycle Bin, or missing with no undo record, were deleted on purpose.

## 2. File naming (enforce README section 4 on every rename)

Source of truth: `README.md` section 4. The audit (`scripts\sonar_index.py audit`, called by `scripts\check.ps1`) checks these exact patterns. A rename that fails the audit is not done.

| Category | Pattern |
|---|---|
| Person documents (Health, Kids, Travel, Legal, `75 <You>`) | `Person_Type_Date_Detail.ext` (no date: `Person_Type_Detail.ext`) |
| IDs and passports | `Person_DocumentType_EXP_YYYY-MM-DD.ext` (expiry always last; underscores between parts, hyphens only inside the ISO date) |
| Unreadable expiry on an ID that expires | `EXP_5555`, only then. No EXP token for docs that don't expire |
| Insurance cards | `Person_Carrier_Side.ext` |
| Portrait photos | `Person_PhotoType_NN.ext` |
| Dated photos (80 Photos) | `Person_Photo_YYYY-MM-DD_Detail.ext` |
| Home (no person) | `Project_Type_Date_Detail.ext` |
| Money (no person) | `YYYY-MM-DD_Source_DocType_Detail.ext` |
| Tax | `YYYY_Tax_Form_Source.ext` |
| Work | `Person_Type_Date_Detail.ext` or `<Employer>_Type_Date.ext` |
| Tools, templates, research | `Topic_Type_Detail.ext` |
| `00 Inbox` | Leave the name as it arrived until filed |

Formatting:
- Dates are `YYYY-MM-DD` (or `YYYY-MM` / `YYYY` when that's all there is).
- Use underscores between parts and hyphens inside a part.
- No spaces. Person and project tokens are CamelCase. Use lowercase extensions. Use straight apostrophes only.
- Banned in final names: `Copy`, `- Copy`, `Old`, `_old`, `(1)`, `.bak`, `final`, `v2`, `DraftN`. Distinguish genuinely different files with a meaningful word: `Filed-Scan`, `PriorFormat`, `OriginalResolution`, `Signed`.
- The date comes from the filename, then page 1 of the document. Never use the file's modified time. `MMDDYYYY_` scanner prefixes are scan dates, not document dates.
- Read page 1 before assigning a person. If you're unsure of the person, date or type, hold the file out and list it with an `Invoke-Item "<full path>"` command.
- Never rename code, dashboards, project folders, saved-page `*_files` folders or generated output. Move them whole. `60 Work\Job Search` is optional and excluded.

## 3. Folder structure

Use the README section 3 template. Documents that never change get one folder (`10 Identity\Passports`). Documents that repeat get a folder per year (`40 Money\Taxes\2025_Tax`, `15 Travel\2025`, `60 Work\Employers\<Employer>\Payslips\2025`). Propose subfolders from what the human actually owns and create them only after approval. Put the person in the filename, not in a folder, except in Health and Kids. A subfolder name always needs a topic word, never a bare year, except under `15 Travel` (the parent folder is the topic). Keep active code and git repos in a separate Projects folder outside the document cabinet so build artifacts and `.git` folders don't bloat OCR and search indexes. When the tree changes, update the cabinet's `README.md` (back it up to `plans\` first).

## 4. Session protocol

- **Start:** read `plans\STATUS.md` and the open rows of `plans\ISSUES.csv`. Reconcile both with the disk and the undo logs. Claim `plans\locks\<folder-slug>.lock` (session id, start time, scope). Skip locked folders. Report locks older than 24h and never take them over silently.
- **Small calls:** do listings, hashes, manifests, audits, logs and STATUS/ISSUES updates without asking. Ask only before applying a manifest or doing anything irreversible.
- **Recommend, don't just ask.** Every flag gets a proposed name or destination and a reason. Group the needs-human items by folder or type, each with an `Invoke-Item "<full path>"` command.
- **Self-review after every apply:** run `scripts\check.ps1 -Folder <path>` and turn every remaining flag into an ISSUES row (`open` if you can fix it under these rules, `needs-human` if it's a judgment call).
- **Definition of done:** the audit is clean (or only needs-human items remain), the session log is in `plans\sessions\`, STATUS is updated and the lock is released. Only then say done.
- **Unattended runs:** don't stop to ask "want me to continue?". Log blockers as needs-human, park the file in `00 Inbox`, keep going, and consolidate the open items at the end.

## 5. Lessons learned (README section 5, condensed)

- `EXP_5555` only for an unreadable expiry. Never invent dates. Scanner prefixes and modified times are not document dates.
- `X_Filed.pdf` and `X_Filed-Copy.pdf` can be different files. Check hashes and keep both under meaningful names.
- Archive wrappers and overlapping extracts can share names without being byte-identical.
- Same-day receipts with `-b` or `-c` suffixes are separate transactions. Don't thin receipts.
- Normalize curly apostrophes to straight ones in filenames.
- A file that's open in a viewer can't be moved. Log it as locked and move on. Never use a header-only undo log from a failed apply.
- Save `.ps1` files that contain non-ASCII characters as UTF-8 with BOM (Windows PowerShell 5.1).
- Add audit exceptions (`plans\check-exceptions.txt`) only for standing exclusions the human approved, never to hide unfinished work.
- OCR: page 1 of a typed scan is read with OCRia Nano and stored on the index row. The PDF is not modified. Skip `65 Learning`, `80 Photos`, and `60 Work` unless asked. Also skip Job search, program files, driver's licenses, x-ray pages, and secret-looking names.
- Work through files on disk (STATUS, ISSUES, session logs) so another agent can pick up where you left off.

## 6. Tools in this repo

- `scripts\sonar.py` (`sonar`): search. `scripts\sonar_app.pyw`: the Sonar desktop app.
- `scripts\sonar_sort.py` (`ssort`): sort a folder with preview, apply and undo.
- `scripts\sonar_index.py`: index, search and audit. `scripts\check.ps1`: audit one folder.
- `scripts\sonar_ocr.py`: page-1 OCRia Nano plan/apply (`--yes` is required to apply). Writes text into the index and does not modify the PDF.
- `scripts\organize.ps1`, `apply-manifest.ps1`, `undo-manifest.ps1`: dry-run-first movers with undo logs.
- All of them read `config.json`. Test changes against a temp cabinet under `%TEMP%` (`--config`, `--dest-root`, `--db`), never against the real cabinet.
