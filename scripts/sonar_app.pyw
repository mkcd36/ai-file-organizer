#!/usr/bin/env python3
"""Sonar window. Stdlib only at import time; preview libraries
are loaded on demand. The index is read-only except when the Sort tab
applies a user-confirmed plan.

Layout: a global Source bar (drive chips with Online/Unplugged health)
sits above the Search, Sort a folder, and Expiring tabs, so every tab
shares one source choice. Keyboard: Ctrl+F or / focuses search, Enter
runs the active tab's primary action, Ctrl+1/2/3 switch tabs.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import Menu, filedialog, messagebox, ttk
from tkinter import font as tkfont

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from sonar_index import load_settings  # noqa: E402
from sonar_preview import PreviewResult, cleanup_temp, preview as build_preview  # noqa: E402
from sonar import (  # noqa: E402
    ALL_SOURCE_LABEL,
    ExpiryItem,
    SearchHit,
    SourceStatus,
    browse,
    configured_sources,
    disconnected_drive_message,
    expiring,
    run_reindex,
    search,
    source_statuses,
)
from sonar_sort import (  # noqa: E402
    ApplyResult,
    SortItem,
    apply_plan,
    latest_undo_log,
    load_rules,
    preview_folder,
    undo_moves,
    write_plan,
)
from sonar_sort import cabinet_root as _cabinet_root_of  # noqa: E402

DOWNLOADS = Path.home() / "Downloads"
_SETTINGS = load_settings()
TOP_FOLDERS = [""] + list(_SETTINGS.get("folders") or [])
PERSONS = [""] + list((_SETTINGS.get("people") or {}).keys())
FILE_TYPES = ["", "PDF", "Image", "Word", "Excel", "Text", "Video", "eBooks", "Other"]

EBOOKS_TYPE = "eBooks"
EXPIRY_PRESETS = (30, 90, 180, 365)
WIDEN_STEPS = (30, 90, 180, 365, 730)
HEALTH_POLL_MS = 30_000
PREVIEW_WIDTH = 360  # right-hand preview pane; drag the divider to resize
UI_POLL_MS = 15
ROW_CHUNK = 120  # rows inserted per Tk tick, so large result sets never block the window

# Worker threads never touch Tk. They post callables here and the main
# thread drains the queue every UI_POLL_MS (see SonarApp._drain_ui_queue).
_UI_QUEUE: "queue.Queue" = queue.Queue()


def post_ui(fn) -> None:
    _UI_QUEUE.put(fn)


def drive_key(path: Path | str) -> str:
    return Path(path).drive.upper() or "/"


def probe_drives(paths) -> dict[str, bool]:
    """{drive: online} for every drive in paths. Touches the disks: call off the main thread."""
    out: dict[str, bool] = {}
    for path in paths:
        key = drive_key(path)
        if key not in out:
            out[key] = disconnected_drive_message(path) is None
    return out


def unplugged_message(key: str) -> str:
    return f"Drive {key} is not connected. Plug in the drive to preview or open this file."
SORT_EMPTY_TITLE = "See where every file would go \u2014 before anything moves."
SORT_EMPTY_HINT = "Pick a folder, then press Scan & Preview. Nothing moves until you apply."
SAFETY_LINE = (
    "Safe by design: nothing is deleted \u00b7 existing files are never overwritten "
    "\u00b7 system files are skipped \u00b7 every apply saves an undo log"
)
CHECK_ON = "\u2611"
CHECK_OFF = "\u2610"
SEARCH_TIPS = """Search tips

  passport              any word, in names and indexed text
  "birth certificate"   exact phrase
  w2 OR 1099            either word
  tax -draft            leave out a word
  payslip*              words starting with "payslip"

Filters
  Folder / Person / Type narrow the results.
  From / To take YYYY-MM-DD (file date or a date in the name).
  eBooks library lists every book, sorted by name.

Sorting
  Click a column header to sort; click again to reverse,
  a third time to return to Relevance. A new query resets
  the order to Relevance.

Keys
  Ctrl+F or /   focus the search box
  Enter         run the active tab's main action
  Ctrl+1/2/3    Search / Sort a folder / Expiring"""

SEARCH_COLUMNS = (
    ("drive", "Drive", 130, False, "w"),
    ("name", "Name", 220, True, "w"),
    ("folder", "Folder", 180, True, "w"),
    ("modified", "Modified", 130, False, "w"),
    ("size", "Size", 80, False, "e"),
    ("snippet", "Snippet", 200, True, "w"),
)


def open_file(path: Path) -> None:
    os.startfile(path)  # type: ignore[attr-defined]


def open_folder(path: Path) -> None:
    subprocess.run(["explorer", f"/select,{path}"], check=False)


def human_size(num: int) -> str:
    if num <= 0:
        return ""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_mtime(mtime_ns: int) -> str:
    if not mtime_ns:
        return ""
    try:
        return datetime.fromtimestamp(mtime_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


class PreviewPane(ttk.Frame):
    """Preview: image, text, or metadata, rendered in a thread."""

    def __init__(self, master: tk.Misc, status_cb=None, blocked_cb=None) -> None:
        super().__init__(master)
        self._gen = 0
        self._path: Path | None = None
        self._result: PreviewResult | None = None
        self._photo = None
        self._source_image = None
        self._resize_job: str | None = None
        self._source_label = ""
        self._note = ""
        self._retry = None
        self._status_cb = status_cb
        self._blocked_cb = blocked_cb
        self._exists = False
        self._build()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        self.title_var = tk.StringVar(value="Preview")
        ttk.Label(self, textvariable=self.title_var, font=("", 10, "bold"), wraplength=PREVIEW_WIDTH - 24).grid(
            row=0, column=0, sticky="ew", padx=10, pady=(8, 2)
        )
        self.details_var = tk.StringVar(value="Select a file to preview.")
        ttk.Label(self, textvariable=self.details_var, wraplength=PREVIEW_WIDTH - 24, justify="left").grid(
            row=1, column=0, sticky="ew", padx=10, pady=(0, 4)
        )
        self.note_var = tk.StringVar(value="")
        self.note_label = ttk.Label(
            self, textvariable=self.note_var, wraplength=PREVIEW_WIDTH - 24, justify="left", style="Note.TLabel"
        )

        btns = ttk.Frame(self)
        btns.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.open_file_btn = ttk.Button(btns, text="Open file", command=self._open_file)
        self.open_file_btn.pack(side="left")
        self.open_folder_btn = ttk.Button(btns, text="Open folder", command=self._open_folder)
        self.open_folder_btn.pack(side="left", padx=(6, 0))
        self.retry_btn = ttk.Button(btns, text="Check again", command=self._on_retry)
        self.prev_btn = ttk.Button(btns, text="Prev", command=self._prev_page, width=6)
        self.next_btn = ttk.Button(btns, text="Next", command=self._next_page, width=6)
        self.page_var = tk.StringVar(value="")
        self.page_label = ttk.Label(btns, textvariable=self.page_var)
        self._set_page_controls(False, 0, 1)
        self._set_open_enabled(False)

        body = ttk.Frame(self)
        body.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 8))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.body = body

        self.image_holder = ttk.Frame(body)
        self.image_holder.columnconfigure(0, weight=1)
        self.image_holder.rowconfigure(0, weight=1)
        self.image_label = ttk.Label(self.image_holder, anchor="center")
        self.image_label.grid(row=0, column=0, sticky="nsew")
        self.image_holder.bind("<Configure>", self._on_image_configure)

        self.text = tk.Text(body, wrap="word", relief="flat", padx=6, pady=6, state="disabled")
        self.text_scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self.text_scroll.set)

        self.message_var = tk.StringVar(value="")
        self.message = ttk.Label(body, textvariable=self.message_var, wraplength=PREVIEW_WIDTH - 24, justify="left")

        self._show_message("Select a file to preview.")
        self._wrap_labels = [w for w in self.winfo_children() if isinstance(w, ttk.Label)] + [self.message]
        self.bind("<Configure>", self._on_resize, add="+")

    def _on_resize(self, event: tk.Event) -> None:
        wrap = max(event.width - 24, 120)
        for label in self._wrap_labels:
            try:
                if int(str(label.cget("wraplength")) or 0) != wrap:
                    label.configure(wraplength=wrap)
            except (tk.TclError, ValueError):
                pass

    def set_note(self, note: str) -> None:
        """One consequence line under the details (for example, where a file will move)."""
        self._note = note or ""
        self.note_var.set(self._note)
        if self._note:
            self.note_label.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
        else:
            self.note_label.grid_forget()

    def _set_retry(self, callback) -> None:
        self._retry = callback
        if callback is not None:
            self.retry_btn.pack(side="left", padx=(12, 0))
        else:
            self.retry_btn.pack_forget()

    def show_path(self, path: Path | None, page: int = 0, source: str = "", note: str | None = None) -> None:
        self._gen += 1
        gen = self._gen
        self._path = path
        self._source_label = source or ""
        self._set_retry(None)
        if note is not None:
            self.set_note(note)
        if path is None:
            self._result = None
            self._source_label = ""
            self.set_note("")
            self.title_var.set("Preview")
            self.details_var.set("Select a file to preview.")
            self._set_open_enabled(False)
            self._set_page_controls(False, 0, 1)
            self._show_message("Select a file to preview.")
            return
        self.title_var.set(f"{path.name}  \u00b7  {source}" if source else path.name)
        self.details_var.set("Loading preview\u2026")
        self._set_open_enabled(False)  # enabled once the worker confirms the file exists
        self._set_page_controls(False, 0, 1)

        def work() -> None:
            try:
                result = build_preview(path, page=page)
            except Exception as exc:
                result = PreviewResult(
                    kind="error",
                    source=path,
                    title=path.name,
                    details=str(path.parent),
                    error=str(exc),
                )
            try:
                exists = result.source.exists()
                has_image = bool(result.image_path and result.image_path.is_file())
            except OSError:
                exists = has_image = False
            post_ui(lambda r=result, g=gen, e=exists, i=has_image: self._apply(r, g, e, i))

        threading.Thread(target=work, daemon=True).start()

    def clear(self) -> None:
        self.show_path(None)

    def show_unavailable(
        self, path: Path, title: str, message: str, details: str = "", on_retry=None
    ) -> None:
        """Show a drive-offline or missing-file message instead of an error.

        Open buttons are disabled; when on_retry is given, a Check again
        button re-tests the drive and re-runs the preview.
        """
        self._gen += 1
        self._path = path
        self._result = None
        self.set_note("")
        self.title_var.set(title or (path.name if path else "Preview"))
        self.details_var.set(details or (str(path) if path else ""))
        self._set_open_enabled(False)
        self._set_page_controls(False, 0, 1)
        self._set_retry(on_retry)
        self._show_message(message)

    def is_unavailable(self) -> bool:
        return self._retry is not None

    def _on_retry(self) -> None:
        if self._retry is not None:
            self._retry()

    def _apply(self, result: PreviewResult, gen: int, exists: bool = False, has_image: bool = False) -> None:
        if gen != self._gen or not self.winfo_exists():
            return
        self._exists = exists
        self._result = result
        self._path = result.source
        title = result.title
        if self._source_label and self._source_label not in title:
            title = f"{title}  \u00b7  {self._source_label}"
        self.title_var.set(title)
        details = result.details
        if self._source_label and details and not details.startswith(self._source_label):
            details = f"{self._source_label}  \u00b7  {details}"
        self.details_var.set(details)
        self._set_open_enabled(exists)
        show_pages = result.kind == "image" and result.page_count > 1
        self._set_page_controls(show_pages, result.page, result.page_count)
        if result.kind == "image" and has_image:
            self._show_image(result.image_path)
            if result.error:
                self.details_var.set(result.details + "  \u00b7  " + result.error)
        elif result.kind == "text":
            self._show_text(result.text)
        else:
            self._show_message(result.error or "No visual preview. Use Open file to view it.")
    def _show_image(self, image_path: Path) -> None:
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self._show_message("Pillow is not installed, so images cannot be displayed.")
            return
        try:
            self._source_image = Image.open(image_path)
            if self._source_image.mode not in {"RGB", "RGBA", "L"}:
                self._source_image = self._source_image.convert("RGB")
        except Exception as exc:
            self._show_message(f"Could not display image: {exc}")
            return
        self._hide_body()
        self.image_holder.grid(row=0, column=0, sticky="nsew")
        self.after(20, self._fit_image)

    def _on_image_configure(self, _event: tk.Event) -> None:
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.after(80, self._fit_image)

    def _fit_image(self) -> None:
        self._resize_job = None
        if self._source_image is None:
            return
        try:
            from PIL import ImageTk
        except ImportError:
            return
        avail_w = max(self.image_holder.winfo_width() - 8, 40)
        avail_h = max(self.image_holder.winfo_height() - 8, 40)
        src_w, src_h = self._source_image.size
        scale = min(avail_w / src_w, avail_h / src_h, 1.0)
        size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
        try:
            resample = __import__("PIL.Image", fromlist=["Image"]).Resampling.LANCZOS
            shown = self._source_image.resize(size, resample) if size != (src_w, src_h) else self._source_image
            self._photo = ImageTk.PhotoImage(shown)
            self.image_label.configure(image=self._photo)
        except Exception as exc:
            self._show_message(f"Could not display image: {exc}")

    def _show_text(self, body: str) -> None:
        self._hide_body()
        self.text.grid(row=0, column=0, sticky="nsew")
        self.text_scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", body)
        self.text.configure(state="disabled")

    def _show_message(self, message: str) -> None:
        self._hide_body()
        self.message_var.set(message)
        self.message.grid(row=0, column=0, sticky="nw", padx=6, pady=6)

    def _hide_body(self) -> None:
        self.image_holder.grid_forget()
        self.text.grid_forget()
        self.text_scroll.grid_forget()
        self.message.grid_forget()

    def _set_open_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.open_file_btn.state(state)
        self.open_folder_btn.state(state)

    def _set_page_controls(self, visible: bool, page: int, count: int) -> None:
        if visible:
            self.prev_btn.pack(side="left", padx=(12, 0))
            self.next_btn.pack(side="left", padx=(4, 0))
            self.page_label.pack(side="left", padx=(8, 0))
            self.page_var.set(f"Page {page + 1} of {count}")
            self.prev_btn.state(["!disabled"] if page > 0 else ["disabled"])
            self.next_btn.state(["!disabled"] if page + 1 < count else ["disabled"])
        else:
            self.prev_btn.pack_forget()
            self.next_btn.pack_forget()
            self.page_label.pack_forget()
            self.page_var.set("")

    def _blocked(self) -> bool:
        message = self._blocked_cb(self._path) if (self._path and self._blocked_cb) else None
        if message and self._status_cb is not None:
            self._status_cb(message)
        return bool(message)

    def _open_file(self) -> None:
        if self._blocked():
            return
        if self._path and self._exists:
            open_file(self._path)

    def _open_folder(self) -> None:
        if self._blocked():
            return
        if self._path:
            open_folder(self._path)

    def _prev_page(self) -> None:
        if self._result and self._path and self._result.page > 0:
            self.show_path(self._path, self._result.page - 1, source=self._source_label)

    def _next_page(self) -> None:
        if self._result and self._path and self._result.page + 1 < self._result.page_count:
            self.show_path(self._path, self._result.page + 1, source=self._source_label)


def _add_tree_and_preview(
    parent: ttk.Frame, preview_width: int = PREVIEW_WIDTH, status_cb=None, blocked_cb=None
) -> tuple[ttk.Frame, PreviewPane, ttk.Panedwindow]:
    """Table on the left, preview on the RIGHT (fixed ~360 px, drag to resize, collapsible)."""
    pane = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
    pane.pack(fill="both", expand=True)
    table = ttk.Frame(pane)
    preview = PreviewPane(pane, status_cb=status_cb, blocked_cb=blocked_cb)
    preview.configure(width=preview_width)
    pane.add(table, weight=1)
    pane.add(preview, weight=0)

    def place_sash(_event: tk.Event | None = None) -> None:
        try:
            width = pane.winfo_width()
            if width > preview_width + 320:
                pane.sashpos(0, width - preview_width)
        except tk.TclError:
            pass
        pane.unbind("<Map>")

    pane.bind("<Map>", place_sash)
    return table, preview, pane


def _scrolled_tree(parent: ttk.Frame, tree: ttk.Treeview) -> None:
    """Vertical + horizontal scrollbars. Columns keep their width as a minimum,
    so a narrow window scrolls sideways instead of squashing every column."""
    for col in tree["columns"]:
        tree.column(col, minwidth=int(tree.column(col, "width")))
    vsb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    parent.rowconfigure(0, weight=1)
    parent.columnconfigure(0, weight=1)


class SonarApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sonar")
        self.geometry("1280x800")
        self.minsize(900, 600)
        self.hits: dict[str, SearchHit] = {}
        self.search_results: list[SearchHit] = []
        self._search_sort: tuple[str | None, bool] = (None, False)
        self._last_query = ""
        self._browsing = False
        self._index_busy = False
        self.sort_items: dict[str, SortItem] = {}
        self.sort_include: dict[str, bool] = {}
        self.sort_plan: Path | None = None
        self._sort_busy = False
        self._sort_applied = False
        self._cabinet: Path | None = None
        self._filter_job = None
        self._health: dict[str, SourceStatus] = {}
        self._health_job = None
        self._tips_win: tk.Toplevel | None = None
        self.expire_items: dict[str, ExpiryItem] = {}
        self.expire_later: list[ExpiryItem] = []
        self._expire_loaded = False
        self._closing = False
        self._drive_states: dict[str, bool] = {}
        self._health_busy = False
        self._health_then: list = []
        self._search_req = 0
        self._search_busy = False
        self._insert_job = None
        self._expire_req = 0
        self._expire_busy = False
        self.timings: dict[str, float] = {}
        self._ui_job = self.after(UI_POLL_MS, self._drain_ui_queue)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self.query_entry.focus_set)

    # ------------------------------------------------------------------ chrome
    def _init_styles(self) -> None:
        style = ttk.Style(self)
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold_font = bold
        style.configure("Accent.TButton", font=bold, foreground="#0b57d0")
        style.configure("Muted.TLabel", foreground="#5f6368")
        style.configure("Note.TLabel", foreground="#0b57d0")
        style.configure("Summary.TLabel", font=bold)
        style.configure("Empty.TLabel", font=bold, foreground="#3c4043")

    def _build(self) -> None:
        self._init_styles()
        self.status = tk.StringVar(value="Ready")
        self._build_source_bar()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        search_tab = ttk.Frame(self.notebook)
        sort_tab = ttk.Frame(self.notebook)
        expire_tab = ttk.Frame(self.notebook)
        self.notebook.add(search_tab, text="Search")
        self.notebook.add(sort_tab, text="Sort a folder")
        self.notebook.add(expire_tab, text="Expiring")
        self._build_search_tab(search_tab)
        self._build_sort_tab(sort_tab)
        self._build_expiring_tab(expire_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        ttk.Label(self, textvariable=self.status, padding=(12, 6, 12, 10), anchor="w").pack(fill="x")
        self._bind_shortcuts()
        self.refresh_health()

    def _build_source_bar(self) -> None:
        """Global Source chips (one per drive, with health) shared by every tab."""
        bar = ttk.Frame(self, padding=(12, 8, 12, 0))
        bar.pack(fill="x")
        self.source_bar = bar
        ttk.Label(bar, text="Source").pack(side="left")
        try:
            self._sources = configured_sources()
        except Exception as exc:
            self._sources = []
            self.status.set(f"Could not read sources: {exc}")
        self.source_var = tk.StringVar(value=ALL_SOURCE_LABEL)
        self.source_chips: dict[str, ttk.Radiobutton] = {}
        for label in [ALL_SOURCE_LABEL] + [s.label for s in self._sources]:
            chip = ttk.Radiobutton(
                bar,
                text=label,
                value=label,
                variable=self.source_var,
                style="Toolbutton",
                command=self.on_source_changed,
            )
            chip.pack(side="left", padx=(6, 0))
            self.source_chips[label] = chip
        self.health_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.health_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))
        self.refresh_btn = ttk.Button(bar, text="Refresh index", command=self.on_refresh)
        self.refresh_btn.pack(side="right")
        ttk.Button(bar, text="Check drives", command=self.refresh_health).pack(side="right", padx=(0, 6))

    # ----------------------------------------------------- background plumbing
    def _drain_ui_queue(self) -> None:
        """Run callbacks posted by worker threads (the only way workers reach Tk)."""
        self._ui_job = None
        deadline = time.perf_counter() + 0.03
        while time.perf_counter() < deadline:
            try:
                fn = _UI_QUEUE.get_nowait()
            except queue.Empty:
                break
            if self._closing:
                continue
            try:
                fn()
            except tk.TclError:
                pass  # widget gone (window closing)
        if not self._closing:
            self._ui_job = self.after(UI_POLL_MS, self._drain_ui_queue)

    @staticmethod
    def _spawn(work) -> None:
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ drive health
    def refresh_health(self, then=None) -> None:
        """Re-test every source in a worker thread, then update the chips.

        The probe touches each drive (a sleeping USB disk can take seconds to
        answer), so it never runs on the Tk main thread.
        """
        if then is not None:
            self._health_then.append(then)
        if self._health_job is not None:
            try:
                self.after_cancel(self._health_job)
            except tk.TclError:
                pass
            self._health_job = None
        if self._health_busy:
            return
        self._health_busy = True
        self.health_var.set("Checking drives\u2026")
        started = time.perf_counter()
        known = list(self._drive_states)

        def work() -> None:
            error = ""
            try:
                statuses = source_statuses()
            except Exception as exc:
                statuses, error = [], str(exc)
            try:
                states = probe_drives([k if k == "/" else k + "\\" for k in known])
            except Exception:
                states = {}
            elapsed = (time.perf_counter() - started) * 1000
            post_ui(lambda: self._health_done(statuses, states, error, elapsed))

        self._spawn(work)

    def _health_done(self, statuses: list[SourceStatus], states: dict[str, bool], error: str, elapsed: float) -> None:
        self._health_busy = False
        self.timings["check_drives_worker_ms"] = elapsed
        before = {k: v.state for k, v in self._health.items()}
        before_drives = dict(self._drive_states)
        self._drive_states.update(states)
        if error:
            self.status.set(f"Could not check drives: {error}")
        self._health = {s.label: s for s in statuses}
        online = sum(1 for s in statuses if s.online)
        for label, chip in self.source_chips.items():
            if label == ALL_SOURCE_LABEL:
                text = f"{label} ({online}/{len(statuses)} online)" if statuses else label
            else:
                status = self._health.get(label)
                if status is None:
                    text = label
                elif status.online:
                    text = f"{label}  \u25cf Online"
                else:
                    text = f"{label}  \u25cb {status.state}"
            chip.configure(text=text)
        offline = [s for s in statuses if s.state == "Unplugged"]
        if offline:
            self.health_var.set(
                "  ".join(
                    f"{s.label} unplugged \u2014 searching its offline index"
                    + (f" from {s.indexed_at}" if s.indexed_at else "")
                    + "."
                    for s in offline
                )
            )
        else:
            self.health_var.set("")
        if before and (before != {k: v.state for k, v in self._health.items()} or before_drives != self._drive_states):
            self._rerender_drive_cells()
        if not self._closing:
            self._health_job = self.after(HEALTH_POLL_MS, self._poll_health)
        callbacks, self._health_then = self._health_then, []
        for cb in callbacks:
            cb()

    def _poll_health(self) -> None:
        self._health_job = None
        self.refresh_health()

    def _rerender_drive_cells(self) -> None:
        """Update only the Drive column (cheap tree.set calls, no disk access)."""
        for item_id, hit in list(self.hits.items()):
            if self.tree.exists(item_id):
                self.tree.set(item_id, "drive", self._drive_cell(hit.source or "", hit.full_path))
        for item_id, item in list(self.expire_items.items()):
            if self.expire_tree.exists(item_id):
                self.expire_tree.set(item_id, "drive", self._drive_cell(item.source or "", item.full_path))

    def _selected_source_key(self) -> str:
        label = (self.source_var.get() or "").strip()
        if not label or label == ALL_SOURCE_LABEL:
            return "all"
        for src in self._sources:
            if src.label == label or src.name == label:
                return src.name
        return "all"

    def on_source_changed(self) -> None:
        if self.query_var.get().strip() or self._browsing:
            self.on_search()
        if self._expire_loaded:
            self.refresh_expiring()

    def cached_unplugged(self, path: Path | None) -> str | None:
        """Unplugged message from the last background probe; never touches the disk."""
        if path is None:
            return None
        key = drive_key(path)
        return unplugged_message(key) if self._drive_states.get(key) is False else None

    def _drive_state(self, path: Path, cache: dict[str, str] | None = None) -> str:
        return "Unplugged" if self._drive_states.get(drive_key(path)) is False else "Online"

    def _drive_cell(self, source: str, path: Path, cache: dict[str, str] | None = None) -> str:
        state = self._drive_state(path)
        return f"{source} \u00b7 {state}" if source else state

    def unplugged_info(self, path: Path, source: str = "") -> tuple[str, str] | None:
        """(message, details) for a file on a drive that is not plugged in, else None."""
        message = self.cached_unplugged(path)
        if not message:
            return None
        status = self._health.get(source)
        stamp = status.indexed_at if status and status.indexed_at else "an earlier scan"
        details = f"{path}\nShowing details from the offline index ({stamp})."
        return message, details

    def _preview_hit(self, hit: SearchHit | ExpiryItem | None, preview: PreviewPane) -> None:
        if hit is None:
            preview.clear()
            return
        title = hit.name if not hit.source else f"{hit.name}  \u00b7  {hit.source}"
        info = self.unplugged_info(hit.full_path, hit.source or "")
        if info:
            message, details = info
            preview.show_unavailable(
                hit.full_path, title, message, details=details, on_retry=lambda p=preview: self._check_again(p)
            )
            self.status.set(message)
            return
        preview.show_path(hit.full_path, source=hit.source or "")

    def _check_again(self, preview: PreviewPane) -> None:
        def reselect() -> None:
            if preview is self.search_preview:
                self.on_search_select()
            elif preview is self.expire_preview:
                self.on_expire_select()

        self.refresh_health(then=reselect)

    def _open_path(self, path: Path, kind: str = "file") -> bool:
        message = self.cached_unplugged(path)
        if message:
            self.status.set(message)
            return False
        try:
            if kind == "folder":
                open_folder(path)
            else:
                open_file(path)
        except OSError as exc:
            self.status.set(f"Could not open {kind}: {exc}")
            return False
        return True

    # --------------------------------------------------------------- shortcuts
    def _bind_shortcuts(self) -> None:
        for seq in ("<Control-f>", "<Control-F>"):
            self.bind(seq, self.focus_search)
        self.bind("<KeyPress-slash>", self.on_slash)
        self.bind("<Return>", self.on_enter)
        self.bind("<KP_Enter>", self.on_enter)
        for i in range(3):
            self.bind(f"<Control-Key-{i + 1}>", lambda _e, n=i: self.select_tab(n))

    def select_tab(self, index: int) -> str:
        self.notebook.select(index)
        if index == 0:
            self.query_entry.focus_set()
        return "break"

    def focus_search(self, _event: object | None = None) -> str:
        self.notebook.select(0)
        self.query_entry.focus_set()
        self.query_entry.select_range(0, "end")
        self.query_entry.icursor("end")
        return "break"

    def on_slash(self, event: tk.Event) -> str | None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)):
            return None  # typing a "/" into a field
        return self.focus_search()

    def active_tab(self) -> int:
        try:
            return int(self.notebook.index("current"))
        except tk.TclError:
            return 0

    def on_enter(self, event: tk.Event | None = None) -> str | None:
        widget = getattr(event, "widget", None)
        if widget in self._own_enter_widgets():
            return None
        self.run_primary_action()
        return "break"

    def _own_enter_widgets(self) -> tuple:
        # Widgets whose own Enter binding already does the right thing.
        return (self.tree, self.filter_from, self.filter_to, self.sort_tree, self.expire_tree)

    def run_primary_action(self) -> None:
        tab = self.active_tab()
        if tab == 0:
            self.on_search()
        elif tab == 1:
            if self._sort_plan_ready() and self._selected_sort_items():
                self.on_apply_selected()
            else:
                self.on_preview_sort()
        else:
            self.refresh_expiring()

    def show_search_tips(self) -> None:
        """Small popover with query syntax, kept out of the search box."""
        if self._tips_win is not None and self._tips_win.winfo_exists():
            self._tips_win.deiconify()
            self._tips_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Search tips")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, text=SEARCH_TIPS, justify="left", font=("Consolas", 9), padding=(14, 12, 14, 6)).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))
        win.bind("<Escape>", lambda _e: win.destroy())
        try:
            x = self.tips_btn.winfo_rootx()
            y = self.tips_btn.winfo_rooty() + self.tips_btn.winfo_height() + 4
            win.geometry(f"+{max(x - 300, 0)}+{y}")
        except tk.TclError:
            pass
        self._tips_win = win

    # ------------------------------------------------------------------ search
    def _build_search_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text="Search").pack(side="left")
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(top, textvariable=self.query_var)
        self.query_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(top, text="Search", command=self.on_search, style="Accent.TButton").pack(side="left")
        self.ebooks_btn = ttk.Button(top, text="eBooks library", command=self.on_ebooks_preset)
        self.ebooks_btn.pack(side="left", padx=(8, 0))
        self.tips_btn = ttk.Button(top, text="Search tips", command=self.show_search_tips)
        self.tips_btn.pack(side="left", padx=(8, 0))

        filters = ttk.Frame(tab, padding=(12, 0, 12, 6))
        filters.pack(fill="x")
        ttk.Label(filters, text="Folder").pack(side="left")
        self.filter_folder = ttk.Combobox(filters, values=TOP_FOLDERS, width=18, state="readonly")
        self.filter_folder.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="Person").pack(side="left")
        self.filter_person = ttk.Combobox(filters, values=PERSONS, width=12, state="readonly")
        self.filter_person.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="Type").pack(side="left")
        self.filter_type = ttk.Combobox(filters, values=FILE_TYPES, width=12, state="readonly")
        self.filter_type.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="From").pack(side="left")
        self.filter_from = ttk.Entry(filters, width=12)
        self.filter_from.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="To").pack(side="left")
        self.filter_to = ttk.Entry(filters, width=12)
        self.filter_to.pack(side="left", padx=(4, 8))
        ttk.Label(filters, text="YYYY-MM-DD", style="Muted.TLabel").pack(side="left")
        self.clear_filters_btn = ttk.Button(filters, text="Clear filters", command=self.on_clear_filters)
        self.clear_filters_btn.pack(side="left", padx=(10, 0))
        self.filter_folder.bind("<<ComboboxSelected>>", self.on_filters_changed)
        self.filter_person.bind("<<ComboboxSelected>>", self.on_filters_changed)
        self.filter_type.bind("<<ComboboxSelected>>", self.on_filters_changed)
        self.filter_from.bind("<Return>", self.on_filters_changed)
        self.filter_to.bind("<Return>", self.on_filters_changed)
        self.filter_from.bind("<FocusOut>", self.on_filters_changed)
        self.filter_to.bind("<FocusOut>", self.on_filters_changed)

        mid = ttk.Frame(tab, padding=(12, 0, 12, 8))
        mid.pack(fill="both", expand=True)
        left, self.search_preview, _pane = _add_tree_and_preview(mid, status_cb=self.status.set, blocked_cb=self.cached_unplugged)

        columns = tuple(c[0] for c in SEARCH_COLUMNS)
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for key, title, width, stretch, anchor in SEARCH_COLUMNS:
            self.tree.heading(key, text=title, command=lambda k=key: self.on_search_heading(k))
            self.tree.column(key, width=width, stretch=stretch, anchor=anchor)
        _scrolled_tree(left, self.tree)

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Menu>", self.on_context_key)
        self.tree.bind("<<TreeviewSelect>>", self.on_search_select)

        self.menu = Menu(self, tearoff=0)
        self.menu.add_command(label="Open file", command=self.menu_open_file)
        self.menu.add_command(label="Open folder", command=self.menu_open_folder)
        self.menu.add_command(label="Copy path", command=self.menu_copy_path)

    def selected_hit(self) -> SearchHit | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.hits.get(selection[0])

    def on_search_select(self, _event: object | None = None) -> None:
        self._preview_hit(self.selected_hit(), self.search_preview)

    def _current_filters(self) -> dict:
        return {
            "folder": self.filter_folder.get() or None,
            "person": self.filter_person.get() or None,
            "file_type": self.filter_type.get() or None,
            "date_from": self.filter_from.get().strip() or None,
            "date_to": self.filter_to.get().strip() or None,
        }

    def on_filters_changed(self, _event: object | None = None) -> str | None:
        if not self.query_var.get().strip() and not self._browsing:
            return None
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except tk.TclError:
                pass
        self._filter_job = self.after(250, self._run_filter_job)
        return "break"

    def _run_filter_job(self) -> None:
        self._filter_job = None
        self.on_search()

    def on_tab_changed(self, _event: object | None = None) -> None:
        if self.active_tab() == 2 and not self._expire_loaded:
            self.refresh_expiring()

    def active_filters(self) -> list[tuple[str, str]]:
        pairs = [
            ("Folder", self.filter_folder.get()),
            ("Person", self.filter_person.get()),
            ("Type", self.filter_type.get()),
            ("From", self.filter_from.get().strip()),
            ("To", self.filter_to.get().strip()),
        ]
        return [(k, v) for k, v in pairs if v]

    def on_clear_filters(self) -> None:
        """Reset Folder/Person/Type/From/To and re-run the search."""
        for combo in (self.filter_folder, self.filter_person, self.filter_type):
            combo.set("")
        for entry in (self.filter_from, self.filter_to):
            entry.delete(0, "end")
        self._browsing = False
        if self.query_var.get().strip():
            self.on_search()
        else:
            self._cancel_insert()
            self._search_req += 1
            self._search_busy = False
            self.search_results = []
            self._render_search()
            self.search_preview.clear()
            self.status.set("Filters cleared.")

    def on_ebooks_preset(self) -> None:
        """One click: Type=eBooks; with an empty query, list the whole library by Name."""
        self.filter_type.set(EBOOKS_TYPE)
        if not self.query_var.get().strip():
            self._search_sort = ("name", False)
        self.on_search()

    def on_search(self, _event: object | None = None) -> str:
        """Start a search in a worker thread; only the newest request may touch the table."""
        query = self.query_var.get().strip()
        if query != self._last_query:
            self._search_sort = (None, False)  # new query: back to Relevance
            self._last_query = query
        self._cancel_insert()
        self._search_req += 1
        req = self._search_req
        params = {
            "folder": self.filter_folder.get() or None,
            "person": self.filter_person.get() or None,
            "file_type": self.filter_type.get() or None,
            "date_from": self.filter_from.get().strip() or None,
            "date_to": self.filter_to.get().strip() or None,
            "source": self._selected_source_key(),
        }
        browsing = not query and params["file_type"] == EBOOKS_TYPE
        if not query and not browsing:
            self._browsing = False
            self._search_busy = False
            self.search_results = []
            self._render_search()
            self.search_preview.clear()
            self.status.set("Type a search and press Enter, or open the eBooks library.")
            return "break"
        self._browsing = browsing
        if browsing and self._search_sort[0] is None:
            self._search_sort = ("name", False)
        self._search_busy = True
        self.status.set("Loading eBooks library\u2026" if browsing else f"Searching for {query!r}\u2026")
        started = time.perf_counter()

        def work() -> None:
            warnings: list[str] = []
            error, mode, hits = "", "like", []
            try:
                if browsing:
                    hits = browse(
                        limit=2000,
                        folder=params["folder"],
                        person=params["person"],
                        file_type=EBOOKS_TYPE,
                        source=params["source"],
                        warnings=warnings,
                    )
                else:
                    hits, mode = search(query, limit=200, warnings=warnings, **params)
            except Exception as exc:
                error = str(exc)
            query_ms = (time.perf_counter() - started) * 1000
            try:
                states = probe_drives(h.full_path for h in hits)
            except Exception:
                states = {}
            post_ui(lambda: self._search_done(req, query, browsing, hits, mode, warnings, error, states, query_ms))

        self._spawn(work)
        return "break"

    def _search_done(self, req, query, browsing, hits, mode, warnings, error, states, query_ms) -> None:
        if req != self._search_req:
            return  # a newer search started; drop this result
        self._search_busy = False
        self.timings["search_worker_ms"] = query_ms
        self._drive_states.update(states)
        self.search_results = hits if not error else []
        if error:
            self._render_search()
            self.search_preview.clear()
            self.status.set(error)
            return
        warn = ("  " + "  ".join(warnings)) if warnings else ""
        if not hits:
            self._render_search()
            self.search_preview.clear()
            active = self.active_filters()
            if active:
                desc = ", ".join(f"{k}={v}" for k, v in active)
                label = "eBooks library" if browsing else repr(query)
                self.status.set(f"0 results for {label} (filters active: {desc} \u2014 click Clear filters).{warn}")
            else:
                self.status.set(f"No results for {query!r}.{warn}")
            return
        if browsing:
            self.status.set(f"eBooks library: {len(hits)} book{'s' if len(hits) != 1 else ''}, sorted by Name.{warn}")
        else:
            note = " (filename match; no FTS hits)" if mode == "like" else ""
            counts: dict[str, int] = {}
            for hit in hits:
                counts[hit.source or "Local"] = counts.get(hit.source or "Local", 0) + 1
            breakdown = ", ".join(f"{name}: {n}" for name, n in counts.items())
            extra = f"  ({breakdown})" if len(counts) > 1 else ""
            active = self.active_filters()
            filt = ("  Filters: " + ", ".join(f"{k}={v}" for k, v in active) + ".") if active else ""
            self.status.set(f"{len(hits)} result{'s' if len(hits) != 1 else ''}{note}{extra}.{filt}{warn}")
        self._render_search(select_first=True)

    def _select_first(self, tree: ttk.Treeview, preview: PreviewPane) -> None:
        first = tree.get_children()
        if first:
            tree.selection_set(first[0])
            tree.focus(first[0])
            tree.see(first[0])
        else:
            preview.clear()

    @staticmethod
    def _search_key(column: str):
        keys = {
            "drive": lambda h: (h.source or "").lower(),
            "name": lambda h: h.name.lower(),
            "folder": lambda h: h.folder.lower(),
            "modified": lambda h: h.mtime_ns,
            "size": lambda h: h.size,
            "snippet": lambda h: (h.snippet or "").lower(),
        }
        return keys[column]

    def sorted_search_results(self) -> list[SearchHit]:
        column, desc = self._search_sort
        if column is None:
            return list(self.search_results)  # Relevance: the order search returned
        return sorted(self.search_results, key=self._search_key(column), reverse=desc)

    def on_search_heading(self, column: str) -> None:
        current, desc = self._search_sort
        if current != column:
            self._search_sort = (column, False)
        elif not desc:
            self._search_sort = (column, True)
        else:
            self._search_sort = (None, False)
        self._render_search(keep_selection=True)
        sort_col, sort_desc = self._search_sort
        if sort_col is None:
            self.status.set("Sorted by Relevance.")
        else:
            title = dict((c[0], c[1]) for c in SEARCH_COLUMNS)[sort_col]
            arrow = "\u25bc" if sort_desc else "\u25b2"
            self.status.set(f"Sorted by {title} {arrow}. Click again to reverse, a third time for Relevance.")

    def _cancel_insert(self) -> None:
        if self._insert_job is not None:
            try:
                self.after_cancel(self._insert_job)
            except tk.TclError:
                pass
            self._insert_job = None

    def _row_values(self, hit: SearchHit) -> tuple:
        return (
            self._drive_cell(hit.source or "", hit.full_path),
            hit.name,
            hit.folder,
            format_mtime(hit.mtime_ns),
            human_size(hit.size),
            hit.snippet,
        )

    def _render_search(self, keep_selection: bool = False, select_first: bool = False) -> None:
        """Fill the table ROW_CHUNK rows per Tk tick so big lists never freeze the window."""
        keep = self.selected_hit() if keep_selection else None
        self._cancel_insert()
        self.tree.delete(*self.tree.get_children())
        self.hits.clear()
        sort_col, sort_desc = self._search_sort
        for key, title, *_rest in SEARCH_COLUMNS:
            arrow = (" \u25bc" if sort_desc else " \u25b2") if key == sort_col else ""
            self.tree.heading(key, text=title + arrow)
        ordered = self.sorted_search_results()
        for i, hit in enumerate(ordered):
            self.hits[str(i)] = hit
        target = None
        if keep is not None:
            target = next((str(i) for i, h in enumerate(ordered) if h is keep), None)
        elif select_first and ordered:
            target = "0"
        self._insert_rows(ordered, 0, target)

    def _insert_rows(self, ordered: list[SearchHit], start: int, target: str | None) -> None:
        self._insert_job = None
        end = min(start + ROW_CHUNK, len(ordered))
        for i in range(start, end):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(ordered[i]))
        if target is not None and self.tree.exists(target) and not self.tree.selection():
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
        if end < len(ordered):
            self._insert_job = self.after(1, lambda: self._insert_rows(ordered, end, target))
        elif target is None and not ordered:
            pass

    def on_double_click(self, _event: object | None = None) -> str | None:
        hit = self.selected_hit()
        if hit is None:
            return None
        self._open_path(hit.full_path, "file")
        return "break"

    def _row_at(self, event: tk.Event) -> str | None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
        return row or (self.tree.selection()[0] if self.tree.selection() else None)

    def on_right_click(self, event: tk.Event) -> None:
        if not self._row_at(event):
            return
        self.menu.tk_popup(event.x_root, event.y_root)

    def on_context_key(self, event: tk.Event) -> None:
        if not self.tree.selection():
            return
        self.menu.tk_popup(event.x_root, event.y_root)

    def menu_open_file(self) -> None:
        self.on_double_click()

    def menu_open_folder(self) -> None:
        hit = self.selected_hit()
        if hit is None:
            return
        self._open_path(hit.full_path, "folder")

    def menu_copy_path(self) -> None:
        hit = self.selected_hit()
        if hit is None:
            return
        self.clipboard_clear()
        self.clipboard_append(str(hit.full_path))
        self.status.set("Path copied.")

    def on_refresh(self) -> None:
        if self._index_busy:
            return
        self._index_busy = True
        self.refresh_btn.state(["disabled"])
        self.status.set("Refreshing index…")

        def work() -> None:
            try:
                code, output = run_reindex(source=self._selected_source_key())
            except Exception as exc:
                message = str(exc)
                post_ui(lambda m=message: self._refresh_done(1, m))
                return
            post_ui(lambda c=code, o=output: self._refresh_done(c, o))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_done(self, code: int, output: str) -> None:
        self._index_busy = False
        self.refresh_btn.state(["!disabled"])
        if not self._closing:
            self.refresh_health()
        if code == 0:
            lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
            last = lines[-1] if lines else "Index updated."
            if any("skipped" in ln.lower() for ln in lines) and any(
                ln.lower().startswith("db:") or "seen=" in ln for ln in lines
            ):
                self.status.set("Index updated, with skipped sources. " + last)
            elif any("skipped" in ln.lower() and "not connected" in ln.lower() for ln in lines) and not any(
                "seen=" in ln or ln.lower().startswith("db:") for ln in lines
            ):
                self.status.set(output.strip().replace("\n", "  ") if output.strip() else last)
            elif last.lower().startswith("db:") or "seen=" in last or "skipped" in last.lower():
                self.status.set(last)
            else:
                self.status.set("Index updated.")
        else:
            detail = output.strip().splitlines()[-1] if output.strip() else f"exit {code}"
            self.status.set(f"Index refresh failed: {detail}")

    # -------------------------------------------------------------------- sort
    def _build_sort_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text="Folder").pack(side="left")
        self.sort_folder = tk.StringVar(value=str(DOWNLOADS))
        self.sort_folder_entry = ttk.Entry(top, textvariable=self.sort_folder)
        self.sort_folder_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(top, text="Browse", command=self.on_browse_folder).pack(side="left")
        self.sort_recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.sort_recursive).pack(
            side="left", padx=(8, 0)
        )
        self.scan_btn = ttk.Button(top, text="Scan & Preview", command=self.on_preview_sort)
        self.scan_btn.pack(side="left", padx=(8, 0))

        show = ttk.Frame(tab, padding=(12, 0, 12, 6))
        show.pack(fill="x")
        ttk.Label(show, text="Show").pack(side="left")
        self.sort_filter_var = tk.StringVar(value="all")
        self.sort_filter_btns: dict[str, ttk.Radiobutton] = {}
        for value, title in (("all", "All"), ("move", "Move"), ("inbox", "Inbox"), ("skip", "Skip")):
            btn = ttk.Radiobutton(
                show,
                text=title,
                value=value,
                variable=self.sort_filter_var,
                style="Toolbutton",
                command=self._render_sort,
            )
            btn.pack(side="left", padx=(6, 0))
            self.sort_filter_btns[value] = btn
        self.sort_summary_var = tk.StringVar(value="")
        ttk.Label(show, textvariable=self.sort_summary_var, style="Muted.TLabel").pack(side="right")

        mid = ttk.Frame(tab, padding=(12, 0, 12, 0))
        mid.pack(fill="both", expand=True)
        left, self.sort_preview, self.sort_pane = _add_tree_and_preview(mid, status_cb=self.status.set, blocked_cb=self.cached_unplugged)

        columns = ("include", "action", "file", "dest", "rule", "confidence")
        self.sort_tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.sort_tree.heading("include", text=CHECK_ON, command=self.on_toggle_all_sort)
        self.sort_tree.heading("action", text="Action")
        self.sort_tree.heading("file", text="File")
        self.sort_tree.heading("dest", text="Destination (in cabinet)")
        self.sort_tree.heading("rule", text="Rule")
        self.sort_tree.heading("confidence", text="Confidence")
        self.sort_tree.column("include", width=44, stretch=False, anchor="center")
        self.sort_tree.column("action", width=70, stretch=False)
        self.sort_tree.column("file", width=200, stretch=True)
        self.sort_tree.column("dest", width=300, stretch=True)
        self.sort_tree.column("rule", width=140, stretch=False)
        self.sort_tree.column("confidence", width=80, stretch=False, anchor="e")
        _scrolled_tree(left, self.sort_tree)
        self.sort_tree.bind("<Button-1>", self.on_sort_click)
        self.sort_tree.bind("<Double-1>", self.on_sort_double_click)
        self.sort_tree.bind("<space>", self.on_sort_space)
        self.sort_tree.bind("<<TreeviewSelect>>", self.on_sort_select)

        self.sort_empty = ttk.Frame(left)
        self.sort_empty_title = tk.StringVar(value=SORT_EMPTY_TITLE)
        self.sort_empty_hint = tk.StringVar(value=SORT_EMPTY_HINT)
        ttk.Label(self.sort_empty, textvariable=self.sort_empty_title, style="Empty.TLabel").pack()
        ttk.Label(self.sort_empty, textvariable=self.sort_empty_hint, style="Muted.TLabel").pack(pady=(4, 0))

        bar = ttk.Frame(tab, padding=(12, 6, 12, 6))
        bar.pack(fill="x")
        self.apply_btn = ttk.Button(bar, text="Apply selected (0)", command=self.on_apply_selected)
        self.apply_btn.pack(side="left")
        ttk.Button(bar, text="Undo last", command=self.on_undo_last).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Open plan CSV", command=self.on_open_plan).pack(side="left", padx=(6, 0))
        self.preview_toggle_btn = ttk.Button(bar, text="Hide preview", command=self.toggle_sort_preview)
        self.preview_toggle_btn.pack(side="left", padx=(6, 0))
        ttk.Label(bar, text=SAFETY_LINE, style="Muted.TLabel").pack(side="left", padx=(14, 0))
        self._render_sort()

    def _sort_plan_ready(self) -> bool:
        return bool(self.sort_items) and not self._sort_applied

    def _update_sort_primary(self) -> None:
        """Scan & Preview is primary until a plan exists; then Apply selected (N) is."""
        count = len(self._selected_sort_items()) if self._sort_plan_ready() else 0
        if self._sort_plan_ready():
            self.scan_btn.configure(style="TButton")
            self.apply_btn.configure(style="Accent.TButton", text=f"Apply selected ({count})")
            self.apply_btn.state(["!disabled"] if count else ["disabled"])
        else:
            self.scan_btn.configure(style="Accent.TButton")
            text = "Applied" if self._sort_applied and self.sort_items else "Apply selected (0)"
            self.apply_btn.configure(style="TButton", text=text)
            self.apply_btn.state(["disabled"])

    def toggle_sort_preview(self) -> None:
        panes = [str(p) for p in self.sort_pane.panes()]
        if str(self.sort_preview) in panes:
            self.sort_pane.forget(self.sort_preview)
            self.preview_toggle_btn.configure(text="Show preview")
        else:
            self.sort_pane.add(self.sort_preview, weight=0)
            self.after_idle(lambda: self._place_preview_sash(self.sort_pane))
            self.preview_toggle_btn.configure(text="Hide preview")

    @staticmethod
    def _place_preview_sash(pane: ttk.Panedwindow) -> None:
        try:
            width = pane.winfo_width()
            if width > PREVIEW_WIDTH + 320:
                pane.sashpos(0, width - PREVIEW_WIDTH)
        except tk.TclError:
            pass

    def _cabinet_root(self) -> Path | None:
        if self._cabinet is None:
            try:
                self._cabinet = Path(_cabinet_root_of(load_rules(), None))
            except Exception:
                return None
        return self._cabinet

    def relative_dest(self, dest: Path | None) -> str:
        """Destination shown relative to the cabinet (65 Learning\\Books\\...)."""
        if dest is None or not str(dest) or str(dest) == ".":
            return "\u2014"
        root = self._cabinet_root()
        for base in ([root, root.parent] if root is not None else []):
            try:
                return str(dest.relative_to(base))
            except ValueError:
                continue
        return str(dest)

    @staticmethod
    def _includable(item: SortItem) -> bool:
        return item.action in {"move", "inbox"}

    def _sort_row_values(self, item_id: str, item: SortItem) -> tuple[str, ...]:
        if self._includable(item):
            mark = CHECK_ON if self.sort_include.get(item_id) else CHECK_OFF
            dest = self.relative_dest(item.dest)
        else:
            mark, dest = "\u2014", "\u2014 (stays where it is)"
        return (
            mark,
            item.action.capitalize(),
            item.source.name,
            dest,
            item.rule,
            f"{item.confidence:.2f}",
        )

    def consequence_text(self, item: SortItem, item_id: str | None = None) -> str:
        """Plain words for what Apply will do with this row."""
        note = f" {item.note}" if item.note else ""
        if item.action == "move":
            text = (
                f"Will move to {item.dest}\n"
                f"Rule {item.rule} matched (confidence {item.confidence:.2f}). "
                "If a file already exists there, this row is skipped."
            )
        elif item.action == "inbox":
            text = (
                f"Will move to {item.dest}\n"
                f"No confident rule match, so it goes to the Inbox (rule {item.rule}, "
                f"confidence {item.confidence:.2f}). If a file already exists there, this row is skipped."
            )
        else:
            return f"Will be skipped \u2014 this file stays where it is.{note}"
        if item_id is not None and not self.sort_include.get(item_id):
            text += "\nNot included: tick the box to apply this row."
        return text + (f"\n{item.note}" if item.note else "")

    def _sort_counts(self) -> dict[str, int]:
        counts = {"move": 0, "inbox": 0, "skip": 0}
        for item in self.sort_items.values():
            counts[item.action] = counts.get(item.action, 0) + 1
        return counts

    def _render_sort(self) -> None:
        selection = self.sort_tree.selection()
        keep = selection[0] if selection else None
        self.sort_tree.delete(*self.sort_tree.get_children())
        wanted = self.sort_filter_var.get() or "all"
        shown = 0
        for item_id, item in self.sort_items.items():
            if wanted != "all" and item.action != wanted:
                continue
            self.sort_tree.insert("", "end", iid=item_id, values=self._sort_row_values(item_id, item))
            shown += 1
        counts = self._sort_counts()
        total = len(self.sort_items)
        titles = {"all": "All", "move": "Move", "inbox": "Inbox", "skip": "Skip"}
        for value, btn in self.sort_filter_btns.items():
            n = total if value == "all" else counts.get(value, 0)
            btn.configure(text=f"{titles[value]} ({n})" if total else titles[value])
        if total:
            selected = len(self._selected_sort_items())
            self.sort_summary_var.set(
                f"{total} files \u00b7 {counts['move']} move \u00b7 {counts['inbox']} to Inbox "
                f"\u00b7 {counts['skip']} skip \u00b7 {selected} selected"
            )
        else:
            self.sort_summary_var.set("")
        if not total:
            self.sort_empty_title.set(SORT_EMPTY_TITLE)
            self.sort_empty_hint.set(SORT_EMPTY_HINT)
            self.sort_empty.place(relx=0.5, rely=0.4, anchor="center")
        elif not shown:
            self.sort_empty_title.set(f"No {titles[wanted]} rows in this plan.")
            self.sort_empty_hint.set("Choose All to see every file.")
            self.sort_empty.place(relx=0.5, rely=0.4, anchor="center")
        else:
            self.sort_empty.place_forget()
        if keep and self.sort_tree.exists(keep):
            self.sort_tree.selection_set(keep)
            self.sort_tree.focus(keep)
        self._update_sort_primary()

    def on_browse_folder(self) -> None:
        current = self.sort_folder.get().strip() or str(DOWNLOADS)
        chosen = filedialog.askdirectory(initialdir=current, title="Choose a folder to sort")
        if chosen:
            self.sort_folder.set(chosen)

    def on_preview_sort(self) -> None:
        if self._sort_busy:
            return
        folder = Path(self.sort_folder.get().strip() or str(DOWNLOADS))
        recursive = bool(self.sort_recursive.get())
        self._sort_busy = True
        self.status.set(f"Previewing {folder}\u2026")

        def work() -> None:
            try:
                items, plan = preview_folder(folder, recursive=recursive)
                error = ""
            except Exception as exc:
                items, plan, error = [], None, str(exc)
            try:
                post_ui(lambda: self._preview_done(items, plan, error))
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, items: list[SortItem], plan: Path | None, error: str) -> None:
        self._sort_busy = False
        self.sort_items.clear()
        self.sort_include.clear()
        self.sort_plan = plan
        self._sort_applied = False
        if error:
            self._render_sort()
            self.sort_preview.clear()
            self.status.set(error)
            return
        for i, item in enumerate(items):
            item_id = str(i)
            self.sort_items[item_id] = item
            self.sort_include[item_id] = self._includable(item)
        self.sort_filter_var.set("all")
        self._render_sort()
        counts = self._sort_counts()
        plan_name = plan.name if plan else ""
        self.status.set(
            f"Plan ready: {len(items)} files  move={counts.get('move', 0)}  "
            f"inbox={counts.get('inbox', 0)}  skip={counts.get('skip', 0)}  {plan_name}"
        )
        self._select_first(self.sort_tree, self.sort_preview)

    def _sort_preview_path(self, item: SortItem | None) -> Path | None:
        if item is None:
            return None
        if item.source.exists():
            return item.source
        if item.dest.exists():
            return item.dest
        return item.source

    def on_sort_select(self, _event: object | None = None) -> None:
        selection = self.sort_tree.selection()
        item_id = selection[0] if selection else None
        item = self.sort_items.get(item_id) if item_id else None
        note = self.consequence_text(item, item_id) if item is not None else ""
        self.sort_preview.show_path(self._sort_preview_path(item), note=note)

    def _toggle_sort_row(self, row: str) -> None:
        item = self.sort_items.get(row)
        if item is None or not self._includable(item) or not self._sort_plan_ready():
            return
        self.sort_include[row] = not self.sort_include.get(row, False)
        self.sort_tree.item(row, values=self._sort_row_values(row, item))
        self._render_sort()
        selection = self.sort_tree.selection()
        if selection and selection[0] == row:
            self.sort_preview.set_note(self.consequence_text(item, row))

    def on_sort_click(self, event: tk.Event) -> str | None:
        if self.sort_tree.identify_region(event.x, event.y) != "cell":
            return None
        column = self.sort_tree.identify_column(event.x)
        row = self.sort_tree.identify_row(event.y)
        if not row or column != "#1":
            return None
        self._toggle_sort_row(row)
        return "break"

    def on_sort_space(self, _event: object | None = None) -> str:
        selection = self.sort_tree.selection()
        if selection:
            self._toggle_sort_row(selection[0])
        return "break"

    def on_toggle_all_sort(self) -> None:
        """Header checkbox: select all visible rows, or clear them if all are selected."""
        if not self._sort_plan_ready():
            return
        rows = [r for r in self.sort_tree.get_children() if self._includable(self.sort_items[r])]
        if not rows:
            return
        value = not all(self.sort_include.get(r) for r in rows)
        for r in rows:
            self.sort_include[r] = value
        self._render_sort()
        self.on_sort_select()

    def on_sort_double_click(self, event: tk.Event) -> None:
        row = self.sort_tree.identify_row(event.y)
        if not row:
            return
        item = self.sort_items.get(row)
        if item is None:
            return
        path = self._sort_preview_path(item)
        if path is None:
            return
        try:
            if path.exists():
                open_file(path)
        except OSError as exc:
            self.status.set(f"Could not open file: {exc}")

    def _selected_sort_items(self) -> list[SortItem]:
        selected: list[SortItem] = []
        for item_id, item in self.sort_items.items():
            if self.sort_include.get(item_id) and self._includable(item):
                selected.append(item)
        return selected

    def on_apply_selected(self) -> None:
        if self._sort_busy:
            return
        if not self._sort_plan_ready() or self.sort_plan is None:
            self.status.set("Scan & Preview a folder first.")
            return
        selected = self._selected_sort_items()
        if not selected:
            self.status.set("Nothing selected to apply.")
            return
        if not messagebox.askyesno(
            "Apply sort",
            f"Move {len(selected)} selected file{'s' if len(selected) != 1 else ''}?\n"
            "Existing destinations will be skipped. Nothing is deleted.",
        ):
            return
        apply_plan_path = self.sort_plan.with_name(self.sort_plan.stem + "-selected.csv")
        write_plan(selected, apply_plan_path)
        self._sort_busy = True
        self.status.set(f"Applying {len(selected)} files\u2026")

        def work() -> None:
            try:
                result = apply_plan(apply_plan_path, selected)
                error = ""
            except Exception as exc:
                result, error = ApplyResult(errors=1), str(exc)
            try:
                post_ui(lambda: self._apply_done(result, error))
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=work, daemon=True).start()

    def _apply_done(self, result: ApplyResult, error: str) -> None:
        self._sort_busy = False
        if error:
            self.status.set(f"Apply failed: {error}")
            return
        self._sort_applied = True
        self._render_sort()
        undo = f"  undo={result.undo_path}" if result.undo_path else ""
        self.status.set(
            f"Apply: moved={result.moved}  skipped={result.skipped}  errors={result.errors}{undo}"
        )
        self.on_sort_select()

    def on_undo_last(self) -> None:
        if self._sort_busy:
            return
        log = latest_undo_log()
        if log is None:
            self.status.set("No sort undo log found.")
            return
        if not messagebox.askyesno("Undo last sort", f"Undo moves in {log.name}?"):
            return
        self._sort_busy = True
        self.status.set(f"Undoing {log.name}…")

        def work() -> None:
            try:
                result = undo_moves(log)
                error = ""
            except Exception as exc:
                result, error = ApplyResult(errors=1), str(exc)
            post_ui(lambda: self._undo_done(result, error, log))

        threading.Thread(target=work, daemon=True).start()

    def _undo_done(self, result: ApplyResult, error: str, log: Path) -> None:
        self._sort_busy = False
        if error:
            self.status.set(f"Undo failed: {error}")
            return
        self.status.set(
            f"Undo {log.name}: restored={result.moved}  skipped={result.skipped}  errors={result.errors}"
        )
        self.on_sort_select()

    def on_open_plan(self) -> None:
        if self.sort_plan is None or not self.sort_plan.is_file():
            self.status.set("No plan CSV yet. Run Preview first.")
            return
        try:
            open_file(self.sort_plan)
        except OSError as exc:
            self.status.set(f"Could not open plan: {exc}")

    # ---------------------------------------------------------------- expiring
    def _build_expiring_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Show past due and due within").pack(side="left")
        self.expire_days = ttk.Entry(top, width=6)
        self.expire_days.insert(0, "180")
        self.expire_days.pack(side="left", padx=(6, 4))
        ttk.Label(top, text="days").pack(side="left")
        for preset in EXPIRY_PRESETS:
            ttk.Button(
                top, text=str(preset), width=4, command=lambda d=preset: self.set_expiry_window(d)
            ).pack(side="left", padx=(6 if preset == EXPIRY_PRESETS[0] else 2, 0))
        ttk.Button(top, text="Refresh", command=self.refresh_expiring, style="Accent.TButton").pack(
            side="left", padx=(12, 0)
        )

        self.expire_summary_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.expire_summary_var, style="Summary.TLabel", padding=(12, 0, 12, 6)).pack(
            fill="x"
        )

        mid = ttk.Frame(tab, padding=(12, 0, 12, 8))
        mid.pack(fill="both", expand=True)
        left, self.expire_preview, _pane = _add_tree_and_preview(mid, status_cb=self.status.set, blocked_cb=self.cached_unplugged)

        columns = ("drive", "person", "name", "expiry", "days", "status", "folder")
        self.expire_tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.expire_tree.heading("drive", text="Drive")
        self.expire_tree.heading("person", text="Person")
        self.expire_tree.heading("name", text="Document")
        self.expire_tree.heading("expiry", text="Expiry")
        self.expire_tree.heading("days", text="Days left")
        self.expire_tree.heading("status", text="Status")
        self.expire_tree.heading("folder", text="Folder")
        self.expire_tree.column("drive", width=130, stretch=False, anchor="w")
        self.expire_tree.column("person", width=100, stretch=False)
        self.expire_tree.column("name", width=240, stretch=True)
        self.expire_tree.column("expiry", width=100, stretch=False)
        self.expire_tree.column("days", width=80, stretch=False, anchor="e")
        self.expire_tree.column("status", width=80, stretch=False)
        self.expire_tree.column("folder", width=180, stretch=True)
        self.expire_tree.tag_configure("overdue", background="#f8d0ce")
        self.expire_tree.tag_configure("soon", background="#fde8c8")
        _scrolled_tree(left, self.expire_tree)
        self.expire_tree.bind("<<TreeviewSelect>>", self.on_expire_select)
        self.expire_tree.bind("<Double-1>", self.on_expire_open)
        self.expire_tree.bind("<Return>", self.on_expire_open)

        bar = ttk.Frame(tab, padding=(12, 6, 12, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Open file", command=self.on_expire_open).pack(side="left")
        ttk.Button(bar, text="Open folder", command=self.on_expire_open_folder).pack(side="left", padx=(6, 0))
        self.widen_btn = ttk.Button(bar, text="Widen", command=self.on_widen_expiry)
        self.expire_more_var = tk.StringVar(value="")
        self.expire_more_label = ttk.Label(bar, textvariable=self.expire_more_var, style="Muted.TLabel")

    def expiry_window(self) -> int | None:
        raw = self.expire_days.get().strip()
        return int(raw) if raw else None

    def set_expiry_window(self, days: int | None) -> None:
        self.expire_days.delete(0, "end")
        if days is not None:
            self.expire_days.insert(0, str(days))
        self.refresh_expiring()

    @staticmethod
    def next_window(window: int, later: list[ExpiryItem]) -> int:
        """Smallest step past the window that brings in at least one more document."""
        if not later:
            return window
        nearest = min(item.days_left for item in later)
        for step in WIDEN_STEPS:
            if step > window and step >= nearest:
                return step
        return max(item.days_left for item in later)

    @staticmethod
    def expiry_label(days_left: int, window: int | None) -> str:
        if days_left < 0:
            return "past due"
        if window is not None and days_left <= window:
            return "soon"
        return ""

    def refresh_expiring(self) -> str:
        try:
            window = self.expiry_window()
        except ValueError:
            self.status.set("Days must be a number.")
            return "break"
        self._expire_req += 1
        req = self._expire_req
        self._expire_busy = True
        self._expire_loaded = True
        source = self._selected_source_key()
        self.status.set("Loading expiring documents\u2026")
        started = time.perf_counter()

        def work() -> None:
            warnings: list[str] = []
            error, items = "", []
            try:
                items = expiring(days=None, source=source, warnings=warnings)
            except Exception as exc:
                error = str(exc)
            try:
                states = probe_drives(i.full_path for i in items)
            except Exception:
                states = {}
            elapsed = (time.perf_counter() - started) * 1000
            post_ui(lambda: self._expiring_done(req, window, items, warnings, error, states, elapsed))

        self._spawn(work)
        return "break"

    def _expiring_done(self, req, window, all_items, warnings, error, states, elapsed) -> None:
        if req != self._expire_req:
            return
        self._expire_busy = False
        self.timings["expiring_worker_ms"] = elapsed
        if error:
            self.status.set(error)
            return
        self._drive_states.update(states)
        self._expire_window = window
        self._expire_warnings = warnings
        self._expire_shown = [i for i in all_items if window is None or i.days_left <= window]
        self.expire_later = [i for i in all_items if window is not None and i.days_left > window]
        self._render_expiring()
        self._select_first(self.expire_tree, self.expire_preview)

    def _render_expiring(self, keep_selection: bool = False) -> None:
        keep = self._selected_expiry() if keep_selection else None
        window = getattr(self, "_expire_window", None)
        self.expire_tree.delete(*self.expire_tree.get_children())
        self.expire_items.clear()
        cache = None
        overdue = soon = 0
        reselect = None
        for i, item in enumerate(getattr(self, "_expire_shown", [])):
            item_id = str(i)
            self.expire_items[item_id] = item
            label = self.expiry_label(item.days_left, window)
            if label == "past due":
                overdue += 1
            elif label == "soon":
                soon += 1
            tag = {"past due": "overdue", "soon": "soon"}.get(label, "")
            self.expire_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    self._drive_cell(item.source or "", item.full_path, cache),
                    item.person,
                    item.name,
                    item.expiry.isoformat(),
                    str(item.days_left),
                    label,
                    item.folder,
                ),
                tags=(tag,) if tag else (),
            )
            if keep is not None and item is keep:
                reselect = item_id
        later = len(self.expire_later)
        window_text = f"due within {window} days" if window is not None else "upcoming"
        summary = f"{overdue} past due \u00b7 {soon} {window_text}"
        if window is None:
            summary = f"{overdue} past due \u00b7 {len(self.expire_items) - overdue} upcoming (all dates)"
        if later:
            summary += f" \u00b7 {later} more after this window"
        self.expire_summary_var.set(summary)
        if later and window is not None:
            new = self.next_window(window, self.expire_later)
            self.expire_more_var.set(f"{later} more after this window")
            self.widen_btn.configure(text=f"Widen to {new} days")
            self.expire_more_label.pack(side="right")
            self.widen_btn.pack(side="right", padx=(0, 8))
        else:
            self.expire_more_var.set("")
            self.widen_btn.pack_forget()
            self.expire_more_label.pack_forget()
        warn = ("  " + "  ".join(getattr(self, "_expire_warnings", []))) if getattr(self, "_expire_warnings", []) else ""
        self.status.set(f"Expiring: {summary}.{warn}")
        if reselect is not None:
            self.expire_tree.selection_set(reselect)
            self.expire_tree.focus(reselect)

    def on_widen_expiry(self) -> None:
        window = getattr(self, "_expire_window", None)
        if window is None or not self.expire_later:
            return
        self.set_expiry_window(self.next_window(window, self.expire_later))

    def _selected_expiry(self) -> ExpiryItem | None:
        selection = self.expire_tree.selection()
        if not selection:
            return None
        return self.expire_items.get(selection[0])

    def on_expire_select(self, _event: object | None = None) -> None:
        self._preview_hit(self._selected_expiry(), self.expire_preview)

    def on_expire_open(self, _event: object | None = None) -> str | None:
        item = self._selected_expiry()
        if item is None:
            return None
        self._open_path(item.full_path, "file")
        return "break"

    def on_expire_open_folder(self) -> None:
        item = self._selected_expiry()
        if item is None:
            return
        self._open_path(item.full_path, "folder")

    # ------------------------------------------------------------------- close
    def destroy(self) -> None:
        self._closing = True
        for job in (self._health_job, self._filter_job, self._insert_job, self._ui_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._health_job = self._filter_job = self._insert_job = self._ui_job = None
        super().destroy()

    def _on_close(self) -> None:
        cleanup_temp()
        self.destroy()


def main() -> int:
    app = SonarApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
