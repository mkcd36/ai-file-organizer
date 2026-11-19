#!/usr/bin/env python3
"""Sonar window. Stdlib only at import time; preview libraries
are loaded on demand. The index is read-only except when the Sort tab
applies a user-confirmed plan.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import Menu, filedialog, messagebox, ttk

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from sonar_index import load_settings  # noqa: E402
from sonar_preview import PreviewResult, cleanup_temp, preview as build_preview  # noqa: E402
from sonar import ExpiryItem, SearchHit, expiring, run_reindex, search  # noqa: E402
from sonar_sort import (  # noqa: E402
    ApplyResult,
    SortItem,
    apply_plan,
    latest_undo_log,
    preview_folder,
    undo_moves,
    write_plan,
)

DOWNLOADS = Path.home() / "Downloads"
_SETTINGS = load_settings()
TOP_FOLDERS = [""] + list(_SETTINGS.get("folders") or [])
PERSONS = [""] + list((_SETTINGS.get("people") or {}).keys())
FILE_TYPES = ["", "PDF", "Image", "Word", "Excel", "Text", "Video", "Other"]


def open_file(path: Path) -> None:
    os.startfile(path)  # type: ignore[attr-defined]


def open_folder(path: Path) -> None:
    subprocess.run(["explorer", f"/select,{path}"], check=False)


class PreviewPane(ttk.Frame):
    """Right-hand preview: image, text, or metadata, rendered in a thread."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._gen = 0
        self._path: Path | None = None
        self._result: PreviewResult | None = None
        self._photo = None
        self._source_image = None
        self._resize_job: str | None = None
        self._build()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self.title_var = tk.StringVar(value="Preview")
        ttk.Label(self, textvariable=self.title_var, font=("", 10, "bold"), wraplength=360).grid(
            row=0, column=0, sticky="ew", padx=10, pady=(8, 2)
        )
        self.details_var = tk.StringVar(value="Select a file to preview.")
        ttk.Label(self, textvariable=self.details_var, wraplength=360, justify="left").grid(
            row=1, column=0, sticky="ew", padx=10, pady=(0, 6)
        )

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.open_file_btn = ttk.Button(btns, text="Open file", command=self._open_file)
        self.open_file_btn.pack(side="left")
        self.open_folder_btn = ttk.Button(btns, text="Open folder", command=self._open_folder)
        self.open_folder_btn.pack(side="left", padx=(6, 0))
        self.prev_btn = ttk.Button(btns, text="Prev", command=self._prev_page, width=6)
        self.next_btn = ttk.Button(btns, text="Next", command=self._next_page, width=6)
        self.page_var = tk.StringVar(value="")
        self.page_label = ttk.Label(btns, textvariable=self.page_var)
        self._set_page_controls(False, 0, 1)
        self._set_open_enabled(False)

        body = ttk.Frame(self)
        body.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
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
        self.message = ttk.Label(body, textvariable=self.message_var, wraplength=360, justify="left")

        self._show_message("Select a file to preview.")

    def show_path(self, path: Path | None, page: int = 0) -> None:
        self._gen += 1
        gen = self._gen
        self._path = path
        if path is None:
            self._result = None
            self.title_var.set("Preview")
            self.details_var.set("Select a file to preview.")
            self._set_open_enabled(False)
            self._set_page_controls(False, 0, 1)
            self._show_message("Select a file to preview.")
            return
        self.title_var.set(path.name)
        self.details_var.set("Loading preview…")
        self._set_open_enabled(path.exists())
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
            self.after(0, lambda r=result, g=gen: self._apply(r, g))

        threading.Thread(target=work, daemon=True).start()

    def clear(self) -> None:
        self.show_path(None)

    def _apply(self, result: PreviewResult, gen: int) -> None:
        if gen != self._gen:
            return
        self._result = result
        self._path = result.source
        self.title_var.set(result.title)
        self.details_var.set(result.details)
        self._set_open_enabled(result.source.exists())
        show_pages = result.kind == "image" and result.page_count > 1
        self._set_page_controls(show_pages, result.page, result.page_count)
        if result.kind == "image" and result.image_path and result.image_path.is_file():
            self._show_image(result.image_path)
            if result.error:
                self.details_var.set(result.details + "  ·  " + result.error)
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

    def _open_file(self) -> None:
        if self._path and self._path.exists():
            open_file(self._path)

    def _open_folder(self) -> None:
        if self._path:
            open_folder(self._path)

    def _prev_page(self) -> None:
        if self._result and self._path and self._result.page > 0:
            self.show_path(self._path, self._result.page - 1)

    def _next_page(self) -> None:
        if self._result and self._path and self._result.page + 1 < self._result.page_count:
            self.show_path(self._path, self._result.page + 1)


def _add_tree_and_preview(parent: ttk.Frame, preview_width: int = 420) -> tuple[ttk.Frame, PreviewPane]:
    pane = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
    pane.pack(fill="both", expand=True)
    left = ttk.Frame(pane)
    preview = PreviewPane(pane)
    pane.add(left, weight=3)
    pane.add(preview, weight=2)

    def place_sash(_event: tk.Event | None = None) -> None:
        try:
            width = pane.winfo_width()
            if width > preview_width + 280:
                pane.sashpos(0, width - preview_width)
        except tk.TclError:
            pass
        pane.unbind("<Map>")

    pane.bind("<Map>", place_sash)
    return left, preview


class SonarApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sonar")
        self.geometry("1280x680")
        self.minsize(900, 480)
        self.hits: dict[str, SearchHit] = {}
        self._index_busy = False
        self.sort_items: dict[str, SortItem] = {}
        self.sort_include: dict[str, bool] = {}
        self.sort_plan: Path | None = None
        self._sort_busy = False
        self._filter_job = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self.query_entry.focus_set)

    def _build(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))

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

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, padding=(12, 6, 12, 10), anchor="w").pack(
            fill="x"
        )

    def _build_search_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text="Search").pack(side="left")
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(top, textvariable=self.query_var)
        self.query_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.query_entry.bind("<Return>", self.on_search)
        self.query_entry.bind("<KP_Enter>", self.on_search)

        ttk.Button(top, text="Search", command=self.on_search).pack(side="left")
        self.refresh_btn = ttk.Button(top, text="Refresh index", command=self.on_refresh)
        self.refresh_btn.pack(side="left", padx=(8, 0))

        filters = ttk.Frame(tab, padding=(12, 0, 12, 6))
        filters.pack(fill="x")
        ttk.Label(filters, text="Folder").pack(side="left")
        self.filter_folder = ttk.Combobox(filters, values=TOP_FOLDERS, width=18, state="readonly")
        self.filter_folder.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="Person").pack(side="left")
        self.filter_person = ttk.Combobox(filters, values=PERSONS, width=12, state="readonly")
        self.filter_person.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="Type").pack(side="left")
        self.filter_type = ttk.Combobox(filters, values=FILE_TYPES, width=10, state="readonly")
        self.filter_type.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="From").pack(side="left")
        self.filter_from = ttk.Entry(filters, width=12)
        self.filter_from.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="To").pack(side="left")
        self.filter_to = ttk.Entry(filters, width=12)
        self.filter_to.pack(side="left", padx=(4, 8))
        self.filter_folder.bind("<<ComboboxSelected>>", self.on_filters_changed)
        self.filter_person.bind("<<ComboboxSelected>>", self.on_filters_changed)
        self.filter_type.bind("<<ComboboxSelected>>", self.on_filters_changed)
        self.filter_from.bind("<Return>", self.on_filters_changed)
        self.filter_to.bind("<Return>", self.on_filters_changed)
        self.filter_from.bind("<FocusOut>", self.on_filters_changed)
        self.filter_to.bind("<FocusOut>", self.on_filters_changed)
        self._filter_job = None

        mid = ttk.Frame(tab, padding=(12, 0, 12, 8))
        mid.pack(fill="both", expand=True)
        left, self.search_preview = _add_tree_and_preview(mid)

        columns = ("name", "folder", "snippet")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("name", text="Name")
        self.tree.heading("folder", text="Folder")
        self.tree.heading("snippet", text="Snippet")
        self.tree.column("name", width=220, stretch=True)
        self.tree.column("folder", width=200, stretch=True)
        self.tree.column("snippet", width=220, stretch=True)

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Menu>", self.on_context_key)
        self.tree.bind("<<TreeviewSelect>>", self.on_search_select)

        self.menu = Menu(self, tearoff=0)
        self.menu.add_command(label="Open file", command=self.menu_open_file)
        self.menu.add_command(label="Open folder", command=self.menu_open_folder)
        self.menu.add_command(label="Copy path", command=self.menu_copy_path)

    def _build_sort_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text="Folder").pack(side="left")
        self.sort_folder = tk.StringVar(value=str(DOWNLOADS))
        folder_entry = ttk.Entry(top, textvariable=self.sort_folder)
        folder_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(top, text="Browse", command=self.on_browse_folder).pack(side="left")
        self.sort_recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.sort_recursive).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(top, text="Preview", command=self.on_preview_sort).pack(side="left", padx=(8, 0))

        mid = ttk.Frame(tab, padding=(12, 0, 12, 0))
        mid.pack(fill="both", expand=True)
        left, self.sort_preview = _add_tree_and_preview(mid)

        columns = ("include", "file", "dest", "rule", "confidence")
        self.sort_tree = ttk.Treeview(
            left, columns=columns, show="headings", selectmode="browse"
        )
        self.sort_tree.heading("include", text="Include")
        self.sort_tree.heading("file", text="File")
        self.sort_tree.heading("dest", text="Proposed destination")
        self.sort_tree.heading("rule", text="Rule")
        self.sort_tree.heading("confidence", text="Confidence")
        self.sort_tree.column("include", width=70, stretch=False, anchor="center")
        self.sort_tree.column("file", width=180, stretch=True)
        self.sort_tree.column("dest", width=260, stretch=True)
        self.sort_tree.column("rule", width=140, stretch=False)
        self.sort_tree.column("confidence", width=80, stretch=False, anchor="e")

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.sort_tree.yview)
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self.sort_tree.xview)
        self.sort_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.sort_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.sort_tree.bind("<Button-1>", self.on_sort_click)
        self.sort_tree.bind("<Double-1>", self.on_sort_double_click)
        self.sort_tree.bind("<<TreeviewSelect>>", self.on_sort_select)

        buttons = ttk.Frame(tab, padding=(12, 8, 12, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Apply selected", command=self.on_apply_selected).pack(side="left")
        ttk.Button(buttons, text="Undo last", command=self.on_undo_last).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Open plan CSV", command=self.on_open_plan).pack(side="left", padx=(8, 0))

    def selected_hit(self) -> SearchHit | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.hits.get(selection[0])

    def on_search_select(self, _event: object | None = None) -> None:
        hit = self.selected_hit()
        self.search_preview.show_path(hit.full_path if hit else None)

    def _current_filters(self) -> dict:
        return {
            "folder": self.filter_folder.get() or None,
            "person": self.filter_person.get() or None,
            "file_type": self.filter_type.get() or None,
            "date_from": self.filter_from.get().strip() or None,
            "date_to": self.filter_to.get().strip() or None,
        }

    def on_filters_changed(self, _event: object | None = None) -> None:
        if not self.query_var.get().strip():
            return
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except tk.TclError:
                pass
        self._filter_job = self.after(250, self.on_search)

    def on_tab_changed(self, _event: object | None = None) -> None:
        try:
            tab = self.notebook.tab(self.notebook.select(), "text")
        except tk.TclError:
            return
        if tab == "Expiring" and not self.expire_tree.get_children():
            self.refresh_expiring()

    def on_search(self, _event: object | None = None) -> None:
        query = self.query_var.get().strip()
        self.tree.delete(*self.tree.get_children())
        self.hits.clear()
        if not query:
            self.search_preview.clear()
            self.status.set("Type a search and press Enter.")
            return
        try:
            hits, mode = search(
                query,
                limit=200,
                folder=self.filter_folder.get() or None,
                person=self.filter_person.get() or None,
                file_type=self.filter_type.get() or None,
                date_from=self.filter_from.get().strip() or None,
                date_to=self.filter_to.get().strip() or None,
            )
        except Exception as exc:
            self.search_preview.clear()
            self.status.set(str(exc))
            return
        for i, hit in enumerate(hits):
            item_id = str(i)
            self.hits[item_id] = hit
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(hit.name, hit.folder, hit.snippet),
            )
        if not hits:
            self.search_preview.clear()
            self.status.set(f"No results for {query!r}.")
            return
        note = " (filename match; no FTS hits)" if mode == "like" else ""
        self.status.set(f"{len(hits)} result{'s' if len(hits) != 1 else ''}{note}.")
        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])
            self.tree.focus(first[0])

    def on_double_click(self, _event: object | None = None) -> None:
        hit = self.selected_hit()
        if hit is None:
            return
        try:
            open_file(hit.full_path)
        except OSError as exc:
            self.status.set(f"Could not open file: {exc}")

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
        try:
            open_folder(hit.full_path)
        except OSError as exc:
            self.status.set(f"Could not open folder: {exc}")

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
                code, output = run_reindex()
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda m=message: self._refresh_done(1, m))
                return
            self.after(0, lambda c=code, o=output: self._refresh_done(c, o))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_done(self, code: int, output: str) -> None:
        self._index_busy = False
        self.refresh_btn.state(["!disabled"])
        if code == 0:
            last = output.strip().splitlines()[-1] if output.strip() else "Index updated."
            self.status.set(last if last.lower().startswith("db:") or "seen=" in last else "Index updated.")
        else:
            detail = output.strip().splitlines()[-1] if output.strip() else f"exit {code}"
            self.status.set(f"Index refresh failed: {detail}")

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
        self.status.set(f"Previewing {folder}…")

        def work() -> None:
            try:
                items, plan = preview_folder(folder, recursive=recursive)
                error = ""
            except Exception as exc:
                items, plan, error = [], None, str(exc)
            self.after(0, lambda: self._preview_done(items, plan, error))

        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, items: list[SortItem], plan: Path | None, error: str) -> None:
        self._sort_busy = False
        self.sort_tree.delete(*self.sort_tree.get_children())
        self.sort_items.clear()
        self.sort_include.clear()
        self.sort_plan = plan
        if error:
            self.sort_preview.clear()
            self.status.set(error)
            return
        counts = {"move": 0, "inbox": 0, "skip": 0}
        for i, item in enumerate(items):
            item_id = str(i)
            include = item.action in {"move", "inbox"}
            self.sort_items[item_id] = item
            self.sort_include[item_id] = include
            counts[item.action] = counts.get(item.action, 0) + 1
            self.sort_tree.insert(
                "",
                "end",
                iid=item_id,
                values=self._sort_row_values(item_id, item),
            )
        plan_name = plan.name if plan else ""
        self.status.set(
            f"Preview: {len(items)} files  move={counts.get('move', 0)}  "
            f"inbox={counts.get('inbox', 0)}  skip={counts.get('skip', 0)}  {plan_name}"
        )
        first = self.sort_tree.get_children()
        if first:
            self.sort_tree.selection_set(first[0])
            self.sort_tree.focus(first[0])
        else:
            self.sort_preview.clear()

    def _sort_row_values(self, item_id: str, item: SortItem) -> tuple[str, str, str, str, str]:
        mark = "Yes" if self.sort_include.get(item_id) else "No"
        return (mark, item.source.name, str(item.dest), item.rule, f"{item.confidence:.2f}")

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
        item = self.sort_items.get(selection[0]) if selection else None
        self.sort_preview.show_path(self._sort_preview_path(item))

    def on_sort_click(self, event: tk.Event) -> str | None:
        if self.sort_tree.identify_region(event.x, event.y) != "cell":
            return None
        column = self.sort_tree.identify_column(event.x)
        row = self.sort_tree.identify_row(event.y)
        if not row or column != "#1":
            return None
        self.sort_include[row] = not self.sort_include.get(row, False)
        item = self.sort_items.get(row)
        if item is not None:
            self.sort_tree.item(row, values=self._sort_row_values(row, item))
        return "break"

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
            if self.sort_include.get(item_id) and item.action in {"move", "inbox"}:
                selected.append(item)
        return selected

    def on_apply_selected(self) -> None:
        if self._sort_busy:
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
        if self.sort_plan is None:
            self.status.set("Preview a folder first.")
            return
        apply_plan_path = self.sort_plan.with_name(self.sort_plan.stem + "-selected.csv")
        write_plan(selected, apply_plan_path)
        self._sort_busy = True
        self.status.set(f"Applying {len(selected)} files…")

        def work() -> None:
            try:
                result = apply_plan(apply_plan_path, selected)
                error = ""
            except Exception as exc:
                result, error = ApplyResult(errors=1), str(exc)
            self.after(0, lambda: self._apply_done(result, error))

        threading.Thread(target=work, daemon=True).start()

    def _apply_done(self, result: ApplyResult, error: str) -> None:
        self._sort_busy = False
        if error:
            self.status.set(f"Apply failed: {error}")
            return
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
            self.after(0, lambda: self._undo_done(result, error, log))

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

    def _build_expiring_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Show past due and due within").pack(side="left")
        self.expire_days = ttk.Entry(top, width=6)
        self.expire_days.insert(0, "180")
        self.expire_days.pack(side="left", padx=(6, 4))
        ttk.Label(top, text="days").pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_expiring).pack(side="left", padx=(12, 0))
        self.expire_days.bind("<Return>", lambda _e: self.refresh_expiring())

        mid = ttk.Frame(tab, padding=(12, 0, 12, 8))
        mid.pack(fill="both", expand=True)
        left, self.expire_preview = _add_tree_and_preview(mid)

        columns = ("person", "name", "expiry", "days", "folder")
        self.expire_tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.expire_tree.heading("person", text="Person")
        self.expire_tree.heading("name", text="Document")
        self.expire_tree.heading("expiry", text="Expiry")
        self.expire_tree.heading("days", text="Days left")
        self.expire_tree.heading("folder", text="Folder")
        self.expire_tree.column("person", width=100, stretch=False)
        self.expire_tree.column("name", width=280, stretch=True)
        self.expire_tree.column("expiry", width=110, stretch=False)
        self.expire_tree.column("days", width=90, stretch=False, anchor="e")
        self.expire_tree.column("folder", width=200, stretch=True)
        self.expire_tree.tag_configure("overdue", background="#f8d0ce")
        self.expire_tree.tag_configure("soon", background="#fde8c8")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.expire_tree.yview)
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self.expire_tree.xview)
        self.expire_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.expire_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.expire_tree.bind("<<TreeviewSelect>>", self.on_expire_select)
        self.expire_tree.bind("<Double-1>", self.on_expire_open)
        self.expire_items: dict[str, ExpiryItem] = {}

        buttons = ttk.Frame(tab, padding=(12, 8, 12, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Open file", command=self.on_expire_open).pack(side="left")
        ttk.Button(buttons, text="Open folder", command=self.on_expire_open_folder).pack(side="left", padx=(8, 0))

    def refresh_expiring(self) -> None:
        raw = self.expire_days.get().strip()
        try:
            days = int(raw) if raw else None
        except ValueError:
            self.status.set("Days must be a number.")
            return
        try:
            items = expiring(days=days)
        except Exception as exc:
            self.status.set(str(exc))
            return
        self.expire_tree.delete(*self.expire_tree.get_children())
        self.expire_items.clear()
        overdue = soon = 0
        for i, item in enumerate(items):
            item_id = str(i)
            self.expire_items[item_id] = item
            if item.days_left < 0:
                tag, overdue = "overdue", overdue + 1
                days_label = f"{item.days_left} (past due)"
            elif item.days_left <= 180:
                tag, soon = "soon", soon + 1
                days_label = str(item.days_left)
            else:
                tag, days_label = "", str(item.days_left)
            self.expire_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(item.person, item.name, item.expiry.isoformat(), days_label, item.folder),
                tags=(tag,) if tag else (),
            )
        self.status.set(
            f"Expiring: {len(items)}  past due={overdue}  within 180 days={soon}"
        )
        first = self.expire_tree.get_children()
        if first:
            self.expire_tree.selection_set(first[0])
            self.expire_tree.focus(first[0])
        else:
            self.expire_preview.clear()

    def _selected_expiry(self) -> ExpiryItem | None:
        selection = self.expire_tree.selection()
        if not selection:
            return None
        return self.expire_items.get(selection[0])

    def on_expire_select(self, _event: object | None = None) -> None:
        item = self._selected_expiry()
        self.expire_preview.show_path(item.full_path if item else None)

    def on_expire_open(self, _event: object | None = None) -> None:
        item = self._selected_expiry()
        if item is None:
            return
        try:
            open_file(item.full_path)
        except OSError as exc:
            self.status.set(f"Could not open file: {exc}")

    def on_expire_open_folder(self) -> None:
        item = self._selected_expiry()
        if item is None:
            return
        try:
            open_folder(item.full_path)
        except OSError as exc:
            self.status.set(f"Could not open folder: {exc}")

    def _on_close(self) -> None:
        cleanup_temp()
        self.destroy()


def main() -> int:
    app = SonarApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
