#!/usr/bin/env python3
"""
Photo Duplicate Finder + TUI Review + Safe Rename (EXIF-aware matching + folder picker)

Adds a modal folder picker so users don't need to type deep paths.

Match order:
  1) Exact stem+ext match
  2) EXIF capture-time match (DateTimeOriginal/DateTimeDigitized) within tolerance
     - camera model boost
     - same ext + same size big boost
  3) Optional legacy substring fallback (toggle)

UI:
  - Drive path input + Pick button
  - SD path input + Pick button
  - Cross-format conversion filters
  - Matches table includes Reason
  - Apply rename prefix (non-destructive)
"""

from __future__ import annotations

import os
import sys
import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict, Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header,
    Footer,
    Static,
    Input,
    Button,
    DataTable,
    SelectionList,
    Switch,
    DirectoryTree,
)
from textual.reactive import reactive
from textual.worker import get_current_worker
from textual.screen import Screen
from textual.message import Message


# -----------------------------
# Config
# -----------------------------

BLACKLIST_EXTENSIONS = {
    ".cop",
    ".cos",
    ".cot",
    ".xmp",
    ".aae",
    ".thm",
    ".db",
    ".ini",
}

EXIF_CANDIDATE_EXTS = {
    ".jpg", ".jpeg", ".tif", ".tiff",
    ".dng",
    ".cr2", ".cr3",
    ".nef",
    ".arw",
    ".raf",
    ".rw2",
    ".orf",
}

try:
    import exifread  # type: ignore
except Exception:
    exifread = None


# -----------------------------
# Models
# -----------------------------

@dataclass(frozen=True)
class FileInfo:
    path: Path
    stem: str
    ext: str
    size: int
    mtime: float
    exif_dt: Optional[int]      # epoch seconds
    camera_model: Optional[str]


@dataclass(frozen=True)
class MatchRow:
    kind: str                   # "EXACT" or "CROSS"
    sd: FileInfo
    drive: FileInfo
    format_type: str            # ".raf -> .jpg" or "" for exact
    reason: str                 # why it matched


# -----------------------------
# Folder picker modal
# -----------------------------

class FolderPicked(Message):
    """Posted to the app when the picker confirms a folder."""
    def __init__(self, sender: Screen, field_id: str, path: Path) -> None:
        super().__init__()
        self.field_id = field_id     # "drive_input" or "sd_input"
        self.path = path


def _windows_roots() -> List[Path]:
    roots: List[Path] = []
    if os.name != "nt":
        return roots
    # Probe common drive letters
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        p = Path(f"{c}:\\")
        try:
            if p.exists():
                roots.append(p)
        except Exception:
            continue
    return roots


class FolderPicker(Screen):
    """Modal folder picker using DirectoryTree."""
    CSS = """
    FolderPicker {
        align: center middle;
    }
    #dialog {
        width: 92%;
        height: 88%;
        border: heavy $primary;
        background: $panel;
        padding: 1;
    }
    #topbar { height: auto; }
    #body { height: 1fr; }
    #tree { height: 1fr; width: 1fr; border: round $accent; }
    #right { width: 40%; height: 1fr; padding-left: 1; }
    #selected { height: auto; border: round $accent; padding: 1; }
    #manual { height: auto; }
    #buttons { height: auto; }
    """

    def __init__(self, field_id: str, start_path: Path) -> None:
        super().__init__()
        self.field_id = field_id
        self.start_path = start_path
        self.selected_path: reactive[Path] = reactive(start_path)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Pick a folder", id="title")

            with Horizontal(id="topbar"):
                yield Button("Home", id="home_btn")
                yield Button("Root", id="root_btn")
                if os.name == "nt":
                    yield Button("Drives", id="drives_btn")
                yield Static("", id="hint")

            with Horizontal(id="body"):
                yield DirectoryTree(self.start_path, id="tree")
                with Vertical(id="right"):
                    yield Static("Selected:", id="selected_label")
                    yield Static(str(self.start_path), id="selected")

                    yield Static("Manual path (optional):")
                    yield Input(placeholder="Type a folder path and press Enter", id="manual")

                    with Horizontal(id="buttons"):
                        yield Button("Use this folder", id="use_btn", variant="primary")
                        yield Button("Cancel", id="cancel_btn")

    def on_mount(self) -> None:
        self._update_selected(self.start_path)
        self.query_one("#hint", Static).update("Enter = open/select • Use this folder = confirm")

    def _update_selected(self, p: Path) -> None:
        self.selected_path = p
        self.query_one("#selected", Static).update(str(p))

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self._update_selected(Path(event.path))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel_btn":
            self.app.pop_screen()
            return

        if bid == "use_btn":
            p = self.selected_path
            self.app.post_message(FolderPicked(self, self.field_id, p))
            self.app.pop_screen()
            return

        if bid == "home_btn":
            p = Path.home()
            self._reset_tree_root(p)
            return

        if bid == "root_btn":
            p = Path("C:\\") if os.name == "nt" else Path("/")
            self._reset_tree_root(p)
            return

        if bid == "drives_btn" and os.name == "nt":
            # Reset to first drive if available
            roots = _windows_roots()
            p = roots[0] if roots else Path("C:\\")
            self._reset_tree_root(p)
            return

    def _reset_tree_root(self, p: Path) -> None:
        # Reuse the existing widget; remounting another #tree can race and duplicate IDs.
        tree = self.query_one("#tree", DirectoryTree)
        tree.path = p
        self._update_selected(p)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "manual":
            return
        raw = event.value.strip()
        if not raw:
            return
        p = Path(raw).expanduser()
        if p.exists() and p.is_dir():
            self._reset_tree_root(p)
        else:
            self.query_one("#hint", Static).update("That path does not exist or is not a folder.")


# -----------------------------
# EXIF helpers
# -----------------------------

def _parse_exif_dt_string(s: str) -> Optional[int]:
    s = s.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp())
        except Exception:
            pass
    return None


def read_exif_quick(path: Path) -> Tuple[Optional[int], Optional[str]]:
    if exifread is None:
        return None, None

    try:
        with path.open("rb") as f:
            tags = exifread.process_file(f, details=False, strict=True)
    except Exception:
        return None, None

    dt_val = None
    model_val = None

    for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
        tag = tags.get(key)
        if tag:
            dt_val = _parse_exif_dt_string(str(tag))
            if dt_val is not None:
                break

    model_tag = tags.get("Image Model")
    if model_tag:
        model_val = str(model_tag).strip() or None

    return dt_val, model_val


# -----------------------------
# Scanning
# -----------------------------

def collect_all_files(directory: Path) -> List[Path]:
    all_files: List[Path] = []
    for root, _, files in os.walk(directory):
        root_path = Path(root)
        for name in files:
            if name.startswith("."):
                continue
            all_files.append(root_path / name)
    return all_files


def scan_chunk_build_info(args: Tuple[List[Path]]) -> List[FileInfo]:
    (paths,) = args
    out: List[FileInfo] = []

    for p in paths:
        try:
            if not p.is_file():
                continue
            st = p.stat()
            ext = p.suffix.lower()
            stem = p.stem
            if ext in BLACKLIST_EXTENSIONS:
                continue

            exif_dt = None
            camera_model = None
            if ext in EXIF_CANDIDATE_EXTS:
                exif_dt, camera_model = read_exif_quick(p)

            out.append(
                FileInfo(
                    path=p,
                    stem=stem,
                    ext=ext,
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    exif_dt=exif_dt,
                    camera_model=camera_model,
                )
            )
        except Exception:
            continue

    return out


def scan_directory_parallel_infos(
    directory: Path,
    description: str,
    emit_progress=None,
) -> List[FileInfo]:
    all_files = collect_all_files(directory)
    total = len(all_files)
    if emit_progress:
        emit_progress(f"Found {total} {description}. Preparing scan…")

    if total == 0:
        return []

    num_processes = min(os.cpu_count() or 4, 8)
    chunk_size = max(total // (num_processes * 4), 250)
    chunks = [all_files[i : i + chunk_size] for i in range(0, total, chunk_size)]

    out: List[FileInfo] = []
    with ProcessPoolExecutor(max_workers=num_processes) as executor:
        futures = [executor.submit(scan_chunk_build_info, (chunk,)) for chunk in chunks]
        completed = 0
        for fut in as_completed(futures):
            out.extend(fut.result())
            completed += 1
            if emit_progress:
                emit_progress(f"Scanning {description}: {completed}/{len(chunks)} chunks")

    return out


# -----------------------------
# Matching
# -----------------------------

def build_drive_indexes(drive_infos: List[FileInfo]) -> Tuple[
    Dict[Tuple[str, str], List[FileInfo]],
    Dict[int, List[FileInfo]],
]:
    by_stem_ext: Dict[Tuple[str, str], List[FileInfo]] = defaultdict(list)
    by_exif_dt: Dict[int, List[FileInfo]] = defaultdict(list)

    for fi in drive_infos:
        by_stem_ext[(fi.stem, fi.ext)].append(fi)
        if fi.exif_dt is not None:
            by_exif_dt[fi.exif_dt].append(fi)

    return dict(by_stem_ext), dict(by_exif_dt)


def score_exif_candidate(sd: FileInfo, drv: FileInfo, dt_delta: int) -> int:
    score = 0
    score += max(0, 100 - 5 * abs(dt_delta))

    if sd.camera_model and drv.camera_model and sd.camera_model == drv.camera_model:
        score += 25

    if sd.ext == drv.ext:
        score += 20
        if sd.size == drv.size:
            score += 50

    return score


def exif_candidates_within_tolerance(
    sd: FileInfo,
    drive_by_exif_dt: Dict[int, List[FileInfo]],
    tolerance_seconds: int,
) -> List[Tuple[FileInfo, int, int]]:
    if sd.exif_dt is None:
        return []

    cands: List[Tuple[FileInfo, int, int]] = []
    base = sd.exif_dt
    for delta in range(-tolerance_seconds, tolerance_seconds + 1):
        bucket = drive_by_exif_dt.get(base + delta)
        if not bucket:
            continue
        for drv in bucket:
            s = score_exif_candidate(sd, drv, delta)
            cands.append((drv, delta, s))

    cands.sort(key=lambda t: t[2], reverse=True)
    return cands


def find_matches_hybrid(
    sd_infos: List[FileInfo],
    drive_infos: List[FileInfo],
    *,
    enable_substring_fallback: bool,
    exif_tolerance_seconds: int,
) -> Tuple[List[MatchRow], List[str]]:
    drive_by_stem_ext, drive_by_exif_dt = build_drive_indexes(drive_infos)

    drive_by_ext_for_sub: Dict[str, List[Tuple[str, FileInfo]]] = defaultdict(list)
    if enable_substring_fallback:
        for fi in drive_infos:
            drive_by_ext_for_sub[fi.ext].append((fi.stem, fi))

    matches: List[MatchRow] = []
    cross_types: set[str] = set()

    for sd in sd_infos:
        if sd.ext in BLACKLIST_EXTENSIONS:
            continue

        # 1) exact stem+ext
        exact_list = drive_by_stem_ext.get((sd.stem, sd.ext), [])
        exact_same_size = [drv for drv in exact_list if drv.size == sd.size and drv.ext not in BLACKLIST_EXTENSIONS]
        if exact_same_size:
            drv = exact_same_size[0]
            matches.append(
                MatchRow(
                    kind="EXACT",
                    sd=sd,
                    drive=drv,
                    format_type="",
                    reason="Exact stem+ext+size match",
                )
            )

        # 2) EXIF correlation
        exif_cands = exif_candidates_within_tolerance(sd, drive_by_exif_dt, exif_tolerance_seconds)

        if exif_cands:
            best_same_ext = next((c for c in exif_cands if c[0].ext == sd.ext), None)
            if best_same_ext and not exact_same_size:
                drv, delta, score = best_same_ext
                reason = f"EXIF time match (Δ{delta}s, score {score})"
                if sd.size == drv.size:
                    reason += " + same size"
                if sd.camera_model and drv.camera_model and sd.camera_model == drv.camera_model:
                    reason += " + same camera"
                matches.append(
                    MatchRow(
                        kind="EXACT",
                        sd=sd,
                        drive=drv,
                        format_type="",
                        reason=reason,
                    )
                )

            # cross-format EXIF matches
            for drv, delta, score in exif_cands[:8]:
                if drv.ext == sd.ext:
                    continue
                if sd.ext in BLACKLIST_EXTENSIONS or drv.ext in BLACKLIST_EXTENSIONS:
                    continue

                fmt = f"{sd.ext} -> {drv.ext}"
                cross_types.add(fmt)

                reason = f"EXIF time match (Δ{delta}s, score {score})"
                if sd.camera_model and drv.camera_model and sd.camera_model == drv.camera_model:
                    reason += " + same camera"

                matches.append(
                    MatchRow(
                        kind="CROSS",
                        sd=sd,
                        drive=drv,
                        format_type=fmt,
                        reason=reason,
                    )
                )

        # 3) substring fallback (legacy) only if no EXIF and no exact match
        if enable_substring_fallback and (sd.exif_dt is None) and (not exact_list):
            sd_stem = sd.stem
            sd_ext = sd.ext

            # exact-ext substring
            for drive_stem, drv in drive_by_ext_for_sub.get(sd_ext, []):
                if sd_stem in drive_stem:
                    matches.append(
                        MatchRow(
                            kind="EXACT",
                            sd=sd,
                            drive=drv,
                            format_type="",
                            reason="Substring stem match (legacy)",
                        )
                    )
                    break

            # cross-ext substring
            for other_ext, items in drive_by_ext_for_sub.items():
                if other_ext == sd_ext:
                    continue
                if sd_ext in BLACKLIST_EXTENSIONS or other_ext in BLACKLIST_EXTENSIONS:
                    continue
                for drive_stem, drv in items:
                    if sd_stem in drive_stem:
                        fmt = f"{sd_ext} -> {other_ext}"
                        cross_types.add(fmt)
                        matches.append(
                            MatchRow(
                                kind="CROSS",
                                sd=sd,
                                drive=drv,
                                format_type=fmt,
                                reason="Substring stem match (legacy)",
                            )
                        )

    return matches, sorted(cross_types)


# -----------------------------
# Rename
# -----------------------------

def rename_file_with_prefix(sd_file: Path, prefix: str) -> Tuple[bool, str]:
    new_name = sd_file.parent / f"{prefix}{sd_file.name}"
    if new_name.exists():
        return False, f"SKIP exists: {new_name.name}"
    try:
        sd_file.rename(new_name)
        return True, f"RENAMED: {sd_file.name} -> {new_name.name}"
    except Exception as e:
        return False, f"ERROR: {sd_file.name}: {e}"


def paths_overlap(a: Path, b: Path) -> bool:
    """True when paths are the same folder or nested."""
    try:
        pa = a.resolve()
        pb = b.resolve()
    except Exception:
        return False
    return pa == pb or pa in pb.parents or pb in pa.parents


def validate_prefix(prefix: str) -> Optional[str]:
    if not prefix:
        return "Prefix is required."
    if prefix in {".", ".."}:
        return "Prefix cannot be '.' or '..'."
    if "/" in prefix or "\\" in prefix:
        return "Prefix cannot contain path separators."
    if prefix.strip() != prefix:
        return "Prefix cannot start or end with spaces."
    if len(prefix) > 64:
        return "Prefix is too long (max 64 chars)."
    if not re.fullmatch(r"[A-Za-z0-9._ -]+", prefix):
        return "Prefix contains unsupported characters."
    return None


# -----------------------------
# Textual App
# -----------------------------

class PhotoDupeTUI(App):
    TITLE = "Photo Duplicate Finder (EXIF-aware TUI)"

    CSS = """
    Screen { layout: vertical; }

    #top { height: auto; padding: 1; }
    #main { height: 1fr; }

    #left { width: 38%; height: 1fr; }
    #right { width: 62%; height: 1fr; }

    #paths { height: auto; }
    #toggles { height: auto; margin-top: 1; }
    #actions { height: auto; margin-top: 1; }
    #status { height: auto; padding: 1 0; }
    #summary { height: auto; padding: 0 0 1 0; }

    DataTable { height: 1fr; }
    SelectionList { height: 1fr; }

    .row { height: auto; }
    .row Input { width: 1fr; min-width: 16; }
    .row Button { width: 10; min-width: 10; margin-left: 1; }
    """

    all_matches: List[MatchRow] = []
    cross_types: List[str] = []
    enabled_cross_types: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="top"):
            with Vertical(id="paths"):
                yield Static("Drive path (destination / archive):")
                with Horizontal(classes="row"):
                    yield Input(placeholder="/Volumes/Photos or D:\\Photos", id="drive_input")
                    yield Button("Pick…", id="pick_drive_btn")

                yield Static("SD card path (source):")
                with Horizontal(classes="row"):
                    yield Input(placeholder="/Volumes/SDCARD or E:\\DCIM", id="sd_input")
                    yield Button("Pick…", id="pick_sd_btn")

                yield Static("Rename prefix (non-destructive):")
                yield Input(value="COPIED_", id="prefix_input")

            with Horizontal(id="toggles"):
                yield Static("Legacy substring fallback:")
                yield Switch(value=False, id="substring_switch")
                yield Static("EXIF tolerance (seconds):")
                yield Input(value="2", id="tol_input")

            with Horizontal(id="actions"):
                yield Button("Scan", id="scan_btn", variant="primary")
                yield Button("Apply rename", id="apply_btn")
                yield Button("Clear", id="clear_btn")

            yield Static("", id="status")
            yield Static("", id="summary")

        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Cross-format filters (toggle conversions)")
                yield SelectionList(id="formats_list")

            with Vertical(id="right"):
                yield Static("Matches (review) — includes Reason")
                yield DataTable(id="matches_table")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#matches_table", DataTable)
        table.add_columns("Type", "SD File", "SD Ext", "Drive File", "Drive Ext", "Conversion", "Reason")
        table.cursor_type = "row"

        if exifread is None:
            self._set_summary("EXIF engine: exifread not installed. Install for best matching: pip install exifread")
        else:
            self._set_summary("EXIF engine: exifread enabled (capture-time matching active).")

        self._set_status("Use Pick… buttons to choose folders, then Scan.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "scan_btn":
            self.action_scan()
        elif bid == "apply_btn":
            self.action_apply_rename()
        elif bid == "clear_btn":
            self.action_clear()
        elif bid == "pick_drive_btn":
            self.action_pick_folder("drive_input")
        elif bid == "pick_sd_btn":
            self.action_pick_folder("sd_input")

    # -----------------
    # Messages
    # -----------------

    def on_folder_picked(self, message: FolderPicked) -> None:
        inp = self.query_one(f"#{message.field_id}", Input)
        inp.value = str(message.path)

    # -----------------
    # Helpers
    # -----------------

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _set_summary(self, msg: str) -> None:
        self.query_one("#summary", Static).update(msg)

    def _read_inputs(self) -> Tuple[Optional[Path], Optional[Path], str, int, bool]:
        drive = self.query_one("#drive_input", Input).value.strip()
        sd = self.query_one("#sd_input", Input).value.strip()
        prefix = self.query_one("#prefix_input", Input).value.strip() or "COPIED_"

        substring = self.query_one("#substring_switch", Switch).value
        tol_raw = self.query_one("#tol_input", Input).value.strip()

        tol = 2
        try:
            tol = int(tol_raw)
            tol = max(0, min(tol, 10))
        except Exception:
            tol = 2

        if not drive or not sd:
            return None, None, prefix, tol, substring

        return Path(drive).expanduser(), Path(sd).expanduser(), prefix, tol, substring

    def _refresh_formats_list(self) -> None:
        sl = self.query_one("#formats_list", SelectionList)
        sl.clear_options()
        if not self.cross_types:
            return

        if not self.enabled_cross_types:
            self.enabled_cross_types = set(self.cross_types)

        sl.add_options([(fmt, fmt, (fmt in self.enabled_cross_types)) for fmt in self.cross_types])

    def _current_filtered_matches(self) -> List[MatchRow]:
        out: List[MatchRow] = []
        for m in self.all_matches:
            if m.kind == "EXACT":
                out.append(m)
            else:
                if m.format_type in self.enabled_cross_types:
                    out.append(m)
        return out

    def _refresh_matches_table(self) -> None:
        table = self.query_one("#matches_table", DataTable)
        table.clear()

        filtered = self._current_filtered_matches()
        for i, m in enumerate(filtered):
            table.add_row(
                m.kind,
                m.sd.path.name,
                m.sd.ext,
                m.drive.path.name,
                m.drive.ext,
                m.format_type,
                m.reason,
                key=str(i),
            )

        counts = Counter(m.kind for m in filtered)
        exact_n = counts.get("EXACT", 0)
        cross_n = counts.get("CROSS", 0)
        self._set_status(
            f"Showing {len(filtered)} matches — Exact: {exact_n} | Cross: {cross_n} | "
            f"Conversions enabled: {len(self.enabled_cross_types)}/{len(self.cross_types)}"
        )

    # -----------------
    # Actions
    # -----------------

    def action_pick_folder(self, field_id: str) -> None:
        # Start picker at current value if valid, else home/root
        raw = self.query_one(f"#{field_id}", Input).value.strip()
        start = None
        if raw:
            p = Path(raw).expanduser()
            if p.exists() and p.is_dir():
                start = p
            else:
                # If a file-like path was pasted, try its parent
                parent = p.parent
                if parent.exists() and parent.is_dir():
                    start = parent

        if start is None:
            start = Path.home() if Path.home().exists() else (Path("C:\\") if os.name == "nt" else Path("/"))

        self.push_screen(FolderPicker(field_id=field_id, start_path=start))

    def action_clear(self) -> None:
        self.all_matches = []
        self.cross_types = []
        self.enabled_cross_types = set()
        self.query_one("#formats_list", SelectionList).clear_options()
        self.query_one("#matches_table", DataTable).clear()
        self._set_status("Cleared. Pick folders and Scan.")

    def action_scan(self) -> None:
        drive_path, sd_path, _, tol, substring = self._read_inputs()
        if drive_path is None or sd_path is None:
            self._set_status("Both Drive path and SD path are required.")
            return
        if not drive_path.exists():
            self._set_status("Drive path does not exist.")
            return
        if not sd_path.exists():
            self._set_status("SD path does not exist.")
            return
        if paths_overlap(drive_path, sd_path):
            self._set_status("Drive path and SD path must be different and not nested.")
            return

        self._set_status("Starting scan…")
        self.run_worker(
            lambda: self._scan_worker(drive_path, sd_path, tol, substring),
            exclusive=True,
            name="scan_worker",
            thread=True,
        )

    def _scan_worker(self, drive_path: Path, sd_path: Path, tol: int, substring: bool) -> None:
        worker = get_current_worker()

        def emit(msg: str) -> None:
            if worker.is_cancelled:
                return
            self.call_from_thread(self._set_status, msg)

        emit(f"Scanning drive: {drive_path}")
        drive_infos = scan_directory_parallel_infos(drive_path, "files on drive", emit_progress=emit)
        if worker.is_cancelled:
            return

        emit(f"Scanning SD card: {sd_path}")
        sd_infos = scan_directory_parallel_infos(sd_path, "files on SD card", emit_progress=emit)
        if worker.is_cancelled:
            return

        if not sd_infos:
            self.call_from_thread(self._set_status, "No files found on SD card.")
            return
        if not drive_infos:
            self.call_from_thread(self._set_status, "No files found on drive.")
            return

        emit("Matching (HYBRID)…")
        matches, cross_types = find_matches_hybrid(
            sd_infos,
            drive_infos,
            enable_substring_fallback=substring,
            exif_tolerance_seconds=tol,
        )

        def done() -> None:
            self.all_matches = matches
            self.cross_types = cross_types
            self.enabled_cross_types = set(cross_types)
            self._refresh_formats_list()
            self._refresh_matches_table()

            if not matches:
                self._set_summary(
                    f"No matches found. EXIF tol={tol}s, substring fallback={'on' if substring else 'off'}."
                )
                return

            exact_total = sum(1 for m in matches if m.kind == "EXACT")
            cross_total = sum(1 for m in matches if m.kind == "CROSS")
            self._set_summary(
                f"Scan complete — matches: {len(matches)} (Exact {exact_total}, Cross {cross_total}). "
                f"EXIF tol={tol}s, substring fallback={'on' if substring else 'off'}."
            )

        self.call_from_thread(done)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        sl = self.query_one("#formats_list", SelectionList)
        enabled = set()
        for opt in sl.options:
            if opt.selected:
                enabled.add(str(opt.value))
        self.enabled_cross_types = enabled
        self._refresh_matches_table()

    def action_apply_rename(self) -> None:
        if not self.all_matches:
            self._set_status("No matches loaded. Scan first.")
            return

        prefix = self.query_one("#prefix_input", Input).value.strip() or "COPIED_"
        prefix_err = validate_prefix(prefix)
        if prefix_err:
            self._set_status(prefix_err)
            return
        filtered = self._current_filtered_matches()
        if not filtered:
            self._set_status("No matches after filters.")
            return

        self._set_status(f"Applying rename prefix '{prefix}'…")
        self.run_worker(
            lambda: self._apply_worker(filtered, prefix),
            exclusive=True,
            name="apply_worker",
            thread=True,
        )

    def _apply_worker(self, filtered: List[MatchRow], prefix: str) -> None:
        worker = get_current_worker()

        # Rename each SD file once
        sd_unique: List[Path] = []
        seen = set()
        for m in filtered:
            if m.sd.path not in seen:
                seen.add(m.sd.path)
                sd_unique.append(m.sd.path)

        total = len(sd_unique)
        ok = 0
        skipped = 0
        errors = 0

        for i, sd_file in enumerate(sd_unique, 1):
            if worker.is_cancelled:
                return
            success, msg = rename_file_with_prefix(sd_file, prefix)
            if success:
                ok += 1
            else:
                if msg.startswith("SKIP"):
                    skipped += 1
                else:
                    errors += 1
            self.call_from_thread(self._set_status, f"{msg} ({i}/{total})")

        self.call_from_thread(
            self._set_status,
            f"Rename complete. Renamed {ok}/{total}. Skipped {skipped}. Errors {errors}. Re-scan to refresh.",
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Photo Duplicate Finder TUI (EXIF-aware + picker)")
    _ = parser.parse_args(argv)
    PhotoDupeTUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
