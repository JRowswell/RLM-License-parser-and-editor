#!/usr/bin/env python3
"""
RLM License Viewer
Parses RLM LICENSE records only.

Handles multi-line LICENSE records, extracts the LICENSE header fields,
preserves all remaining information, shows validity/seat totals, and exports CSV.

Two modes in one file:
  GUI  (needs Tkinter + a display):   python3 RLM_License_Viewer_ver7.py
  CLI  (headless, no Tkinter needed): python3 RLM_License_Viewer_ver7.py PATH [--csv OUT] [--filter TEXT]

With no arguments the GUI starts if possible; otherwise usage help is shown.

CLI deletion of expired LICENSE records (never touches valid/permanent ones):
  ... PATH --delete-expired --dry-run   preview only, nothing changes
  ... PATH --delete-expired             preview, then asks you to type 'yes'
  ... PATH --delete-expired --yes       skip the prompt (for scripts)
A timestamped .bak backup of each file is made before it is modified.
Requires Python 3.8+.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # headless box without python3-tk
    tk = None

if sys.platform.startswith("win"):
    UI_FONT, MONO_FONT = "Segoe UI", "Consolas"
elif sys.platform == "darwin":
    UI_FONT, MONO_FONT = "Helvetica Neue", "Menlo"
else:
    UI_FONT, MONO_FONT = "DejaVu Sans", "DejaVu Sans Mono"

DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y")

@dataclass
class LicenseRecord:
    source_file: str
    start_line: int
    raw_record: str
    isv: str = ""
    product: str = ""
    version: str = ""
    expiry_text: str = ""
    expiry_date: date | None = None
    quantity: int = 0
    header_extra: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def status(self):
        if self.expiry_date is None:
            return "VALID"
        return "VALID" if self.expiry_date >= date.today() else "EXPIRED"

    @property
    def attribute_text(self):
        return " ".join(f"{k}={v}" for k, v in self.attributes.items())

CSV_HEADER = [
    "Status", "Source File", "Start Line", "ISV", "Product",
    "Version", "Expiry", "Amount", "Attributes", "Complete LICENSE Record"
]

def csv_row(r):
    return [
        r.status, r.source_file, r.start_line, r.isv, r.product,
        r.version, r.expiry_text, r.quantity, r.attribute_text, r.raw_record
    ]

def record_matches(record, query):
    """Case-insensitive substring match across all record text."""
    if not query:
        return True
    searchable = " ".join([
        record.source_file, record.isv, record.product, record.version,
        record.expiry_text, record.attribute_text, record.raw_record
    ]).lower()
    return query.lower() in searchable

def delete_license_records(records):
    """Remove the given LICENSE records from their source files.

    Each file is verified first (the lines at the recorded position must still
    match what was parsed), then backed up to <name>.bak-<timestamp>, then
    rewritten. If verification fails for a file, that file is left untouched.
    Line endings are preserved. Returns (deleted_count, errors, backups).
    """
    by_file = {}
    for record in records:
        by_file.setdefault(Path(record.source_file), []).append(record)

    deleted, errors, backups = 0, [], []

    for path, recs in by_file.items():
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                original = f.read()
            lines = original.splitlines(keepends=True)

            # Work backwards so earlier line numbers remain valid.
            file_deleted = 0
            for record in sorted(recs, key=lambda r: r.start_line, reverse=True):
                start = record.start_line - 1
                count = len(record.raw_record.splitlines())
                if start < 0 or start + count > len(lines):
                    raise RuntimeError(
                        f"Could not safely locate LICENSE record starting at line "
                        f"{record.start_line}."
                    )

                actual = [ln.rstrip() for ln in lines[start:start + count]]
                expected = record.raw_record.splitlines()
                if actual != expected:
                    raise RuntimeError(
                        f"File changed since it was loaded; LICENSE record at "
                        f"line {record.start_line} no longer matches."
                    )

                del lines[start:start + count]
                file_deleted += 1

            # Timestamped backup, only once the edit is known to be safe.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.bak-{stamp}")
            counter = 1
            while backup.exists():
                backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
                counter += 1
            shutil.copy2(path, backup)
            backups.append(str(backup))

            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("".join(lines))
            deleted += file_deleted

        except Exception as exc:
            errors.append(f"{path}: {exc}")

    return deleted, errors, backups

TOP_LEVEL_RE = re.compile(
    r"^(?:HOST|ISV|LICENSE|OPTIONS|INCLUDE|ROAM|REMOTE|GROUP|RESERVE|MAX|TIMEOUT|"
    r"DEBUG|REPORTLOG|LOG|NOLOG|ACTLOG|RLM|UPGRADE)\b", re.IGNORECASE
)

LICENSE_RE = re.compile(
    r"^\s*LICENSE\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$",
    re.IGNORECASE
)

def parse_rlm_date(value):
    value = value.strip().strip('"')
    if value.lower() in {"permanent", "perpetual", "none", "never", "-"}:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None

def split_license_records(text):
    lines = text.splitlines()
    records = []
    current = None
    current_start = None

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()

        if re.match(r"^LICENSE\b", stripped, re.IGNORECASE):
            if current is not None:
                records.append((current_start, "\n".join(current)))
            current = [line.rstrip()]
            current_start = lineno
            continue

        if current is not None:
            # RLM continuation lines in the supplied license are indented.
            # Blank lines are retained too. A new non-indented top-level
            # directive ends the current LICENSE record.
            if stripped == "" or line[:1].isspace():
                current.append(line.rstrip())
            elif TOP_LEVEL_RE.match(stripped):
                records.append((current_start, "\n".join(current)))
                current = None
                current_start = None
            else:
                current.append(line.rstrip())

    if current is not None:
        records.append((current_start, "\n".join(current)))

    return records

def parse_license_record(source_file, start_line, raw):
    logical = re.sub(r"\s+", " ", raw).strip()
    match = LICENSE_RE.match(logical)
    if not match:
        return None

    isv, product, version, expiry_text, quantity_text, extra = match.groups()

    try:
        quantity = int(quantity_text)
    except ValueError:
        quantity = 0

    header_extra = (extra or "").strip()
    attributes = {}

    # Parse key=value pairs, including quoted signatures.
    attr_re = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"[^"]*"|\S+)')
    for m in attr_re.finditer(header_extra):
        key = m.group(1)
        value = m.group(0).split("=", 1)[1]
        attributes[key] = value

    return LicenseRecord(
        source_file=source_file,
        start_line=start_line,
        raw_record=raw,
        isv=isv,
        product=product,
        version=version,
        expiry_text=expiry_text,
        expiry_date=parse_rlm_date(expiry_text),
        quantity=quantity,
        header_extra=header_extra,
        attributes=attributes,
    )

def parse_file(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [
        r for start_line, raw in split_license_records(text)
        if (r := parse_license_record(str(path), start_line, raw))
    ]

def find_license_files(path):
    path = Path(path)
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".lic")

class RLMViewer(tk.Tk if tk else object):
    def __init__(self):
        super().__init__()
        self.title("RLM License Viewer")
        self.geometry("1500x850")
        self.minsize(1100, 650)

        # Dark mode colours.
        self.dark_bg = "#1e1e1e"
        self.dark_panel = "#252526"
        self.dark_input = "#2d2d30"
        self.dark_fg = "#f1f1f1"
        self.dark_muted = "#b8b8b8"
        self.dark_border = "#3f3f46"
        self.dark_select = "#264f78"
        self.dark_valid = "#6ccb7a"
        self.dark_expired = "#ff6b6b"

        self.configure(bg=self.dark_bg)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".",
            background=self.dark_bg,
            foreground=self.dark_fg,
            fieldbackground=self.dark_input,
            bordercolor=self.dark_border,
            lightcolor=self.dark_border,
            darkcolor=self.dark_border,
        )
        style.configure(
            "TFrame",
            background=self.dark_bg,
        )
        style.configure(
            "TLabelframe",
            background=self.dark_bg,
            foreground=self.dark_fg,
            bordercolor=self.dark_border,
        )
        style.configure(
            "TLabelframe.Label",
            background=self.dark_bg,
            foreground=self.dark_fg,
        )
        style.configure(
            "TLabel",
            background=self.dark_bg,
            foreground=self.dark_fg,
        )
        style.configure(
            "TButton",
            background=self.dark_panel,
            foreground=self.dark_fg,
            bordercolor=self.dark_border,
            padding=(8, 5),
        )
        style.map(
            "TButton",
            background=[("active", "#333337"), ("pressed", "#3a3a3e")],
            foreground=[("disabled", "#777777")],
        )
        style.configure(
            "TEntry",
            fieldbackground=self.dark_input,
            foreground=self.dark_fg,
            insertcolor=self.dark_fg,
            bordercolor=self.dark_border,
        )
        style.configure(
            "TScrollbar",
            background=self.dark_panel,
            troughcolor=self.dark_bg,
            bordercolor=self.dark_border,
            arrowcolor=self.dark_fg,
        )
        style.configure(
            "Treeview",
            background=self.dark_input,
            foreground=self.dark_fg,
            fieldbackground=self.dark_input,
            bordercolor=self.dark_border,
            rowheight=25,
        )
        style.configure(
            "Treeview.Heading",
            background=self.dark_panel,
            foreground=self.dark_fg,
            bordercolor=self.dark_border,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", self.dark_select)],
            foreground=[("selected", "#ffffff")],
        )

        self.records = []
        self.filtered_records = []
        self.current_path = None
        self.sort_column = None
        self.sort_reverse = False
        self.build_ui()

    def build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Button(top, text="Open .LIC File", command=self.open_file).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Add .LIC to Existing", command=self.add_lic_to_existing).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(top, text="Scan Folder", command=self.scan_folder).pack(side="left", padx=(0, 12))
        ttk.Button(top, text="Export CSV", command=self.export_csv).pack(side="left", padx=(0, 8))
        ttk.Button(top, text="Delete Selected Expired", command=self.delete_selected_expired).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(top, text="Select All Expired", command=self.select_all_expired).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(top, text="RLM Reread...", command=self.rlm_reread).pack(
            side="left", padx=(0, 18)
        )

        ttk.Label(top, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.filter_var, width=40)
        entry.pack(side="left", padx=6)
        entry.bind("<KeyRelease>", lambda _e: self.refresh_table())
        ttk.Button(top, text="Clear", command=self.clear_filter).pack(side="left")

        summary = ttk.Frame(self, padding=(8, 0, 8, 8))
        summary.pack(fill="x")
        self.summary_labels = {}
        for name in ("Files", "LICENSE Records", "Valid", "Expired", "Valid Seats", "All Seats"):
            box = ttk.LabelFrame(summary, text=name, padding=(12, 7))
            box.pack(side="left", padx=(0, 8))
            label = ttk.Label(box, text="0", font=(UI_FONT, 14, "bold"))
            label.pack()
            self.summary_labels[name] = label

        frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        frame.pack(fill="both", expand=True)

        columns = ("status", "file", "line", "isv", "product", "version", "expiry", "quantity", "attributes")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")

        headings = {
            "status": "Status", "file": "File", "line": "Line", "isv": "ISV",
            "product": "Product", "version": "Version", "expiry": "Expiry",
            "quantity": "Amount", "attributes": "LICENSE attributes"
        }
        widths = {
            "status": 90, "file": 230, "line": 55, "isv": 100, "product": 180,
            "version": 105, "expiry": 115, "quantity": 75, "attributes": 550
        }
        self.columns = columns
        self.headings = headings

        for col in columns:
            self.tree.heading(
                col,
                text=headings[col],
                command=lambda c=col: self.sort_by_column(c)
            )
            self.tree.column(col, width=widths[col], anchor="w")

        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("valid", foreground=self.dark_valid)
        self.tree.tag_configure("expired", foreground=self.dark_expired)
        self.tree.bind("<<TreeviewSelect>>", self.show_selected_record)
        self.tree.bind("<Double-1>", self.show_selected_record)

        detail_frame = ttk.LabelFrame(self, text="Complete LICENSE record", padding=8)
        detail_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.detail = tk.Text(
            detail_frame,
            height=10,
            wrap="none",
            font=(MONO_FONT, 10),
            bg=self.dark_input,
            fg=self.dark_fg,
            insertbackground=self.dark_fg,
            selectbackground=self.dark_select,
            selectforeground="#ffffff",
            relief="flat",
            borderwidth=1,
        )
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail.yview)
        detail_scroll.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=detail_scroll.set)

        self.status_var = tk.StringVar(value="Open an RLM .lic file or scan a folder.")
        tk.Label(
            self,
            textvariable=self.status_var,
            bg=self.dark_panel,
            fg=self.dark_muted,
            relief="sunken",
            anchor="w",
            padx=8,
            pady=4,
        ).pack(fill="x")

    def sort_by_column(self, column):
        """Sort the LICENSE table by the clicked column.

        Clicking the same heading toggles ascending/descending order.
        Clicking a different heading starts with ascending order.
        """
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False

        key_functions = {
            "status": lambda r: r.status.upper(),
            "file": lambda r: os.path.basename(r.source_file).lower(),
            "line": lambda r: r.start_line,
            "isv": lambda r: r.isv.lower(),
            "product": lambda r: r.product.lower(),
            "version": lambda r: r.version.lower(),
            "expiry": lambda r: (
                r.expiry_date if r.expiry_date is not None else date.max
            ),
            "quantity": lambda r: r.quantity,
            "attributes": lambda r: r.attribute_text.lower(),
        }

        key = key_functions[column]
        self.records.sort(key=key, reverse=self.sort_reverse)

        self.update_heading_indicators()
        self.refresh_table()

    def update_heading_indicators(self):
        """Show an up/down arrow on the currently sorted heading."""
        for col in self.columns:
            label = self.headings[col]
            if col == self.sort_column:
                label += "  " + ("▼" if self.sort_reverse else "▲")
            self.tree.heading(
                col,
                text=label,
                command=lambda c=col: self.sort_by_column(c)
            )

    def clear_filter(self):
        self.filter_var.set("")
        self.refresh_table()

    def open_file(self):
        filename = filedialog.askopenfilename(
            title="Open RLM License File",
            filetypes=[("RLM license files", "*.lic"), ("All files", "*.*")]
        )
        if filename:
            self.load_path(Path(filename))

    def add_lic_to_existing(self):
        """Append LICENSE records from a selected .lic file to an existing .lic file."""
        source_filename = filedialog.askopenfilename(
            title="Select .LIC File to Add",
            filetypes=[("RLM license files", "*.lic"), ("All files", "*.*")]
        )
        if not source_filename:
            return

        source_path = Path(source_filename)

        # Read and validate the source file before asking for the destination.
        try:
            source_text = source_path.read_text(encoding="utf-8", errors="replace")
            source_records = [
                parse_license_record(str(source_path), start_line, raw)
                for start_line, raw in split_license_records(source_text)
            ]
            source_records = [r for r in source_records if r is not None]
        except Exception as exc:
            messagebox.showerror(
                "Add .LIC",
                f"Could not read the selected license file:\n\n{source_path}\n\n{exc}"
            )
            return

        if not source_records:
            messagebox.showwarning(
                "Add .LIC",
                f"No valid LICENSE records were found in:\n\n{source_path}"
            )
            return

        destination_filename = filedialog.askopenfilename(
            title="Select Existing .LIC File to Append To",
            filetypes=[("RLM license files", "*.lic"), ("All files", "*.*")]
        )
        if not destination_filename:
            return

        destination_path = Path(destination_filename)

        if source_path.resolve() == destination_path.resolve():
            messagebox.showwarning(
                "Add .LIC",
                "The source and destination files are the same file."
            )
            return

        try:
            destination_text = destination_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except Exception as exc:
            messagebox.showerror(
                "Add .LIC",
                f"Could not read the destination license file:\n\n"
                f"{destination_path}\n\n{exc}"
            )
            return

        # Display what is about to be added.
        preview = []
        for record in source_records:
            preview.append(
                f"{record.isv} / {record.product} / {record.version} / "
                f"{record.expiry_text} / {record.quantity} seat(s)"
            )

        answer = messagebox.askyesno(
            "Confirm Add .LIC",
            f"Append {len(source_records)} LICENSE record(s) from:\n"
            f"{source_path}\n\n"
            f"to:\n{destination_path}\n\n"
            "A backup of the destination file will be created first.\n\n"
            "Records to add:\n"
            + "\n".join(preview[:30])
            + (f"\n...and {len(preview) - 30} more." if len(preview) > 30 else "")
        )
        if not answer:
            return

        try:
            # Always make a backup before modifying the destination.
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = destination_path.with_name(
                f"{destination_path.name}.bak-{stamp}"
            )
            counter = 1
            while backup.exists():
                backup = destination_path.with_name(
                    f"{destination_path.name}.bak-{stamp}-{counter}"
                )
                counter += 1

            shutil.copy2(destination_path, backup)

            # Preserve the destination exactly, only ensuring there is a newline
            # before the first appended LICENSE record.
            separator = ""
            if destination_text and not destination_text.endswith(("\n", "\r")):
                separator = "\n"

            appended_records = []
            for record in source_records:
                raw = record.raw_record.rstrip("\r\n")
                appended_records.append(raw + "\n")

            with destination_path.open("a", encoding="utf-8") as f:
                f.write(separator)
                f.write("".join(appended_records))

        except Exception as exc:
            messagebox.showerror(
                "Add .LIC failed",
                f"Could not append the LICENSE records:\n\n{exc}"
            )
            return

        messagebox.showinfo(
            "Add .LIC complete",
            f"Added {len(source_records)} LICENSE record(s) to:\n"
            f"{destination_path}\n\n"
            f"Backup created:\n{backup}"
        )

        # Reload the destination if it is the currently loaded file.
        if self.current_path is not None:
            try:
                current = Path(self.current_path).resolve()
                if current == destination_path.resolve():
                    self.load_path(destination_path)
            except Exception:
                pass

    def scan_folder(self):
        folder = filedialog.askdirectory(title="Select Folder Containing RLM .lic Files")
        if folder:
            self.load_path(Path(folder))

    def load_path(self, path):
        files = find_license_files(path)
        if not files:
            messagebox.showinfo("RLM License Viewer", "No .lic files were found.")
            return

        records, errors = [], []
        for file_path in files:
            try:
                records.extend(parse_file(file_path))
            except Exception as exc:
                errors.append(f"{file_path}: {exc}")

        self.current_path = path
        self.records = records
        self.refresh_table()

        msg = f"Loaded {len(files)} file(s), {len(records)} LICENSE record(s)."
        if errors:
            msg += f" {len(errors)} file(s) could not be read."
            messagebox.showwarning("Some files could not be read", msg + "\n\n" + "\n".join(errors[:10]))
        self.status_var.set(msg)

    def refresh_table(self):
        query = self.filter_var.get().strip().lower()
        self.filtered_records = []

        for record in self.records:
            if record_matches(record, query):
                self.filtered_records.append(record)

        for item in self.tree.get_children():
            self.tree.delete(item)

        for index, record in enumerate(self.filtered_records):
            self.tree.insert(
                "", "end", iid=str(index),
                values=(
                    record.status,
                    os.path.basename(record.source_file),
                    record.start_line,
                    record.isv,
                    record.product,
                    record.version,
                    record.expiry_text,
                    record.quantity,
                    record.attribute_text,
                ),
                tags=("valid" if record.status == "VALID" else "expired",)
            )

        self.update_summary()

    def update_summary(self):
        files = len({r.source_file for r in self.records})
        valid = [r for r in self.records if r.status == "VALID"]
        expired = [r for r in self.records if r.status == "EXPIRED"]

        self.summary_labels["Files"].configure(text=str(files))
        self.summary_labels["LICENSE Records"].configure(text=str(len(self.records)))
        self.summary_labels["Valid"].configure(text=str(len(valid)))
        self.summary_labels["Expired"].configure(text=str(len(expired)))
        self.summary_labels["Valid Seats"].configure(text=str(sum(r.quantity for r in valid)))
        self.summary_labels["All Seats"].configure(text=str(sum(r.quantity for r in self.records)))

    def show_selected_record(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        record = self.filtered_records[int(selection[0])]
        self.detail.delete("1.0", "end")
        self.detail.insert(
            "1.0",
            f"Source: {record.source_file}\n"
            f"Starts at file line: {record.start_line}\n"
            f"Status: {record.status}\n"
            f"ISV: {record.isv}\n"
            f"Product: {record.product}\n"
            f"Version: {record.version}\n"
            f"Expiry: {record.expiry_text}\n"
            f"Amount: {record.quantity}\n\n"
            f"{record.raw_record}\n"
        )

    def select_all_expired(self):
        """Select every currently visible expired LICENSE record."""
        self.tree.selection_remove(self.tree.selection())
        expired_iids = [
            str(index)
            for index, record in enumerate(self.filtered_records)
            if record.status == "EXPIRED"
        ]
        if expired_iids:
            self.tree.selection_set(expired_iids)
            self.tree.focus(expired_iids[0])
            self.show_selected_record()
            self.status_var.set(f"Selected {len(expired_iids)} expired LICENSE record(s).")
        else:
            self.status_var.set("No expired LICENSE records are visible with the current filter.")

    def delete_selected_expired(self):
        """Remove selected expired LICENSE records from their source .lic files."""
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(
                "Delete Expired Licenses",
                "Select one or more expired LICENSE records first."
            )
            return

        selected = [self.filtered_records[int(iid)] for iid in selection]

        # Never silently delete a valid license.
        non_expired = [r for r in selected if r.status != "EXPIRED"]
        if non_expired:
            messagebox.showwarning(
                "Valid LICENSE selected",
                "One or more selected records are not expired.\n\n"
                "Only expired LICENSE records can be deleted. "
                "Please select expired records only."
            )
            return

        # Group by source file and show exactly what will be removed.
        by_file = {}
        for record in selected:
            by_file.setdefault(Path(record.source_file), []).append(record)

        preview = []
        for path, records in by_file.items():
            preview.append(f"{path}")
            for record in records:
                preview.append(
                    f"  Line {record.start_line}: "
                    f"{record.isv} / {record.product} / {record.version} / "
                    f"{record.expiry_text} / {record.quantity} seat(s)"
                )

        answer = messagebox.askyesno(
            "Confirm deletion",
            f"Delete {len(selected)} expired LICENSE record(s) from the .lic file(s)?\n\n"
            "A backup will be created before each file is changed.\n\n"
            + "\n".join(preview[:40])
            + (f"\n...and {len(preview) - 40} more line(s)." if len(preview) > 40 else "")
        )
        if not answer:
            return

        deleted, errors, backups = delete_license_records(selected)

        if errors:
            messagebox.showerror(
                "Deletion error",
                "Some files could not be updated:\n\n" + "\n".join(errors)
                + ("\n\nBackups created:\n" + "\n".join(backups) if backups else "")
            )
        else:
            messagebox.showinfo(
                "Deletion complete",
                f"Deleted {deleted} expired LICENSE record(s).\n\n"
                "Backups created:\n" + "\n".join(backups)
            )

        # Reload the currently selected file/folder so line numbers and totals are correct.
        if self.current_path is not None and not errors:
            self.load_path(self.current_path)

    def _find_rlmutil(self, folder):
        """Find rlmutil in a selected RLM installation directory."""
        folder = Path(folder)

        candidates = [
            folder / "rlmutil",
            folder / "rlmutil.exe",
            folder / "bin" / "rlmutil",
            folder / "bin" / "rlmutil.exe",
        ]

        for candidate in candidates:
            if candidate.is_file():
                return candidate

        # Also check one directory level below the selected installation folder.
        try:
            for candidate in folder.glob("*/rlmutil*"):
                if candidate.is_file() and candidate.name.lower() in {"rlmutil", "rlmutil.exe"}:
                    return candidate
        except OSError:
            pass

        return None

    def rlm_reread(self):
        """Run rlmutil rlmreread using a user-selected RLM installation folder."""
        folder = filedialog.askdirectory(
            title="Select RLM Installation Folder (containing rlmutil)"
        )
        if not folder:
            return

        rlmutil = self._find_rlmutil(folder)
        if rlmutil is None:
            messagebox.showerror(
                "RLM Reread",
                "Could not find rlmutil or rlmutil.exe in the selected folder.\n\n"
                "Select the RLM installation folder containing the RLM utilities."
            )
            return

        # RLM documents: rlmutil rlmreread [isv].
        # With no ISV supplied, RLM and all ISV servers are asked to reread.
        command = [str(rlmutil), "rlmreread"]

        try:
            result = subprocess.run(
                command,
                cwd=str(rlmutil.parent),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            messagebox.showerror(
                "RLM Reread",
                f"Could not run:\n{rlmutil}\n\n{exc}"
            )
            return

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if not output:
            output = "(rlmutil returned no output)"

        if result.returncode == 0:
            messagebox.showinfo(
                "RLM Reread",
                f"RLM reread command completed successfully.\n\n"
                f"Command:\n{' '.join(command)}\n\n"
                f"Output:\n{output}"
            )
            self.status_var.set("RLM reread completed successfully.")
        else:
            messagebox.showerror(
                "RLM Reread Failed",
                f"rlmutil returned exit code {result.returncode}.\n\n"
                f"Command:\n{' '.join(command)}\n\n"
                f"Output:\n{output}"
            )
            self.status_var.set(f"RLM reread failed (exit code {result.returncode}).")

    def export_csv(self):
        if not self.records:
            messagebox.showinfo("Export CSV", "There are no LICENSE records to export.")
            return

        filename = filedialog.asksaveasfilename(
            title="Export RLM License Data",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")]
        )
        if not filename:
            return

        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            for r in self.records:
                writer.writerow(csv_row(r))

        self.status_var.set(f"Exported {len(self.records)} LICENSE records to {filename}")
        messagebox.showinfo("Export complete", f"Exported {len(self.records)} LICENSE records.")

def has_display():
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True  # Windows / macOS

def run_cli(args):
    files = find_license_files(args.path)
    if not files:
        print(f"No .lic files found at {args.path}", file=sys.stderr)
        return 1

    records = []
    for f in files:
        try:
            records.extend(parse_file(f))
        except Exception as exc:
            print(f"warning: {f}: {exc}", file=sys.stderr)

    query = args.filter.strip()
    records = [r for r in records if record_matches(r, query)]

    if args.delete_expired:
        return run_cli_delete(args, records)

    if args.csv:
        out = sys.stdout if args.csv == "-" else open(args.csv, "w", newline="", encoding="utf-8-sig")
        try:
            writer = csv.writer(out)
            writer.writerow(CSV_HEADER)
            for r in records:
                writer.writerow(csv_row(r))
        finally:
            if out is not sys.stdout:
                out.close()
    else:
        print(f"{'STATUS':8} {'ISV':10} {'PRODUCT':22} {'VER':8} {'EXPIRY':12} {'QTY':>5}  FILE:LINE")
        for r in records:
            print(f"{r.status:8} {r.isv:10} {r.product:22} {r.version:8} "
                  f"{r.expiry_text:12} {r.quantity:>5}  {r.source_file}:{r.start_line}")

    valid = [r for r in records if r.status == "VALID"]
    # Summary goes to stderr so piped CSV output stays clean.
    print(f"\n{len(files)} file(s), {len(records)} records, {len(valid)} valid, "
          f"{len(records) - len(valid)} expired, "
          f"{sum(r.quantity for r in valid)} valid seats, "
          f"{sum(r.quantity for r in records)} total seats", file=sys.stderr)
    return 0

def run_cli_delete(args, records):
    expired = [r for r in records if r.status == "EXPIRED"]
    if not expired:
        print("No expired LICENSE records match; nothing to delete.")
        return 0

    by_file = {}
    for r in expired:
        by_file.setdefault(r.source_file, []).append(r)

    print(f"{len(expired)} expired LICENSE record(s) selected for deletion:\n")
    for path, recs in by_file.items():
        print(path)
        for r in sorted(recs, key=lambda x: x.start_line):
            print(f"  Line {r.start_line}: {r.isv} / {r.product} / {r.version} / "
                  f"{r.expiry_text} / {r.quantity} seat(s)")
    print("\nValid and permanent licenses are never deleted.")

    if args.dry_run:
        print("Dry run: no files were changed.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to delete without confirmation: run interactively, "
                  "or pass --yes.", file=sys.stderr)
            return 2
        try:
            answer = input(f"\nType 'yes' to delete these {len(expired)} record(s) "
                           f"(a backup of each file is made first): ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Aborted; no files were changed.")
            return 1

    deleted, errors, backups = delete_license_records(expired)
    if backups:
        print("\nBackups created:")
        for b in backups:
            print(f"  {b}")
    print(f"Deleted {deleted} expired LICENSE record(s).")
    if errors:
        print("\nSome files could not be updated:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0

def run_gui(initial_path=None):
    app = RLMViewer()
    if initial_path:
        app.after(100, lambda: app.load_path(Path(initial_path)))
    app.mainloop()
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description="RLM License Viewer (GUI and headless CLI)")
    ap.add_argument("path", nargs="?", help=".lic file or folder to scan")
    ap.add_argument("--csv", metavar="OUT", help="write CSV to this file ('-' for stdout)")
    ap.add_argument("--filter", default="", help="case-insensitive substring filter")
    ap.add_argument("--gui", action="store_true", help="open the GUI (optionally preloading PATH)")
    ap.add_argument("--delete-expired", action="store_true",
                    help="delete expired LICENSE records (after --filter) from the .lic files; "
                         "asks for confirmation and makes a .bak backup first")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --delete-expired: show what would be deleted, change nothing")
    ap.add_argument("--yes", action="store_true",
                    help="with --delete-expired: skip the confirmation prompt")
    args = ap.parse_args(argv)

    if (args.dry_run or args.yes) and not args.delete_expired:
        ap.error("--dry-run and --yes only apply with --delete-expired")
    if args.delete_expired and (args.csv or args.gui or args.path is None):
        ap.error("--delete-expired needs a PATH and cannot be combined with --csv or --gui")

    want_gui = args.gui or (args.path is None and not args.csv)
    if want_gui:
        if tk is None:
            print("Tkinter is not installed; use CLI mode (give a PATH).", file=sys.stderr)
            ap.print_usage(sys.stderr)
            return 2
        if not has_display():
            print("No display available; use CLI mode (give a PATH).", file=sys.stderr)
            ap.print_usage(sys.stderr)
            return 2
        return run_gui(args.path)

    if args.path is None:
        ap.error("a PATH is required with --csv")
    return run_cli(args)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # e.g. piped into head/less
        try:
            sys.stdout.close()
        except Exception:
            pass
        os._exit(0)
