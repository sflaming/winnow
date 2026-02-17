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
import json
import hashlib
import time
import subprocess
import threading
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict, Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
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
    Select,
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

PARTIAL_HASH_BYTES = 1024 * 1024
PARTIAL_HASH_CHUNK = 256 * 1024
FULL_HASH_CHUNK = 1024 * 1024
QUARANTINE_DIR_NAME = ".photo_dupe_quarantine"
TX_LOG_NAME = ".photo_dupe_transactions.jsonl"
CONFIRM_WINDOW_SECONDS = 8.0
RECENT_PATHS_FILE = Path.home() / ".photo_dupe_recent_paths.json"
MAX_RECENT_PATHS = 12
RAW_PREVIEW_EXTENSIONS = {
    ".raf",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".rw2",
    ".orf",
    ".dng",
}
IMAGE_PREVIEW_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}
PREVIEW_CACHE_DIR = Path(tempfile.gettempdir()) / "photo_dupe_preview_cache"
PREVIEW_CHARS_WIDTH = 48
PREVIEW_CHARS_HEIGHT = 14
PROCESS_POOL_BROKEN = False
PROCESS_POOL_BROKEN_REASON = ""

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
    exif_dt: Optional[int] = None       # epoch seconds
    camera_model: Optional[str] = None
    lens_model: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


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


class QuarantineViewer(Screen):
    """Read-only view of files currently in quarantine."""

    CSS = """
    QuarantineViewer {
        align: center middle;
    }
    #q_dialog {
        width: 92%;
        height: 88%;
        border: heavy $primary;
        background: $panel;
        padding: 1;
    }
    #q_table { height: 1fr; border: round $accent; }
    #q_actions { height: auto; }
    """

    def __init__(self, quarantine_root: Path) -> None:
        super().__init__()
        self.quarantine_root = quarantine_root

    def compose(self) -> ComposeResult:
        with Vertical(id="q_dialog"):
            yield Static(f"Quarantine: {self.quarantine_root}")
            yield DataTable(id="q_table")
            with Horizontal(id="q_actions"):
                yield Button("Refresh", id="q_refresh_btn")
                yield Button("Close", id="q_close_btn", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#q_table", DataTable)
        table.add_columns("File", "Relative Path", "Size", "Modified")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#q_table", DataTable)
        table.clear()

        if not self.quarantine_root.exists():
            return

        files = [p for p in self.quarantine_root.rglob("*") if p.is_file()]
        files.sort(key=lambda p: str(p).lower())
        for i, p in enumerate(files):
            try:
                rel = p.relative_to(self.quarantine_root)
            except Exception:
                rel = Path(p.name)
            try:
                st = p.stat()
                size_txt = format_size(int(st.st_size))
                mtime_txt = datetime.fromtimestamp(float(st.st_mtime)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                size_txt = "?"
                mtime_txt = "?"
            table.add_row(p.name, str(rel), size_txt, mtime_txt, key=str(i))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "q_close_btn":
            self.app.pop_screen()
        elif event.button.id == "q_refresh_btn":
            self._refresh_table()


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


def _parse_int_tag_value(tag_value: str) -> Optional[int]:
    m = re.search(r"\d+", tag_value)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def read_exif_quick(path: Path) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[int], Optional[int]]:
    if exifread is None:
        return None, None, None, None, None

    try:
        with path.open("rb") as f:
            tags = exifread.process_file(f, details=False, strict=True)
    except Exception:
        return None, None, None, None, None

    dt_val = None
    model_val = None
    lens_val = None
    width_val = None
    height_val = None

    for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
        tag = tags.get(key)
        if tag:
            dt_val = _parse_exif_dt_string(str(tag))
            if dt_val is not None:
                break

    model_tag = tags.get("Image Model")
    if model_tag:
        model_val = str(model_tag).strip() or None

    lens_tag = tags.get("EXIF LensModel") or tags.get("EXIF LensSpecification")
    if lens_tag:
        lens_val = str(lens_tag).strip() or None

    width_tag = tags.get("EXIF ExifImageWidth") or tags.get("Image ImageWidth")
    if width_tag:
        width_val = _parse_int_tag_value(str(width_tag))

    height_tag = tags.get("EXIF ExifImageLength") or tags.get("Image ImageLength")
    if height_tag:
        height_val = _parse_int_tag_value(str(height_tag))

    return dt_val, model_val, lens_val, width_val, height_val


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

            exif_dt = None
            camera_model = None
            lens_model = None
            width = None
            height = None
            if ext in EXIF_CANDIDATE_EXTS:
                exif_dt, camera_model, lens_model, width, height = read_exif_quick(p)

            out.append(
                FileInfo(
                    path=p,
                    stem=stem,
                    ext=ext,
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    exif_dt=exif_dt,
                    camera_model=camera_model,
                    lens_model=lens_model,
                    width=width,
                    height=height,
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
    global PROCESS_POOL_BROKEN, PROCESS_POOL_BROKEN_REASON
    all_files = collect_all_files(directory)
    total = len(all_files)
    if emit_progress:
        emit_progress(f"Found {total} {description}. Preparing scan…")

    if total == 0:
        return []

    num_processes = min(os.cpu_count() or 4, 8)
    chunk_size = max(total // (num_processes * 4), 250)
    chunks = [all_files[i : i + chunk_size] for i in range(0, total, chunk_size)]

    def run_with_executor(executor_cls) -> List[FileInfo]:
        out: List[FileInfo] = []
        with executor_cls(max_workers=num_processes) as executor:
            futures = [executor.submit(scan_chunk_build_info, (chunk,)) for chunk in chunks]
            completed = 0
            for fut in as_completed(futures):
                out.extend(fut.result())
                completed += 1
                if emit_progress:
                    emit_progress(f"Scanning {description}: {completed}/{len(chunks)} chunks")
        return out

    # In TUI we scan from a thread worker; process pools from non-main threads are
    # unreliable on some Python/macOS builds (fds_to_keep / semaphore issues).
    if threading.current_thread() is not threading.main_thread():
        return run_with_executor(ThreadPoolExecutor)

    if PROCESS_POOL_BROKEN:
        if emit_progress and PROCESS_POOL_BROKEN_REASON:
            emit_progress(f"Process workers unavailable ({PROCESS_POOL_BROKEN_REASON}). Using thread workers.")
        return run_with_executor(ThreadPoolExecutor)

    try:
        return run_with_executor(ProcessPoolExecutor)
    except Exception as e:
        PROCESS_POOL_BROKEN = True
        PROCESS_POOL_BROKEN_REASON = f"{type(e).__name__}: {e}"
        if emit_progress:
            emit_progress(
                f"Process workers unavailable ({PROCESS_POOL_BROKEN_REASON}). Using thread workers."
            )
        return run_with_executor(ThreadPoolExecutor)


# -----------------------------
# Hash / Keep helpers
# -----------------------------

def partial_hash_file(path: Path) -> Optional[str]:
    try:
        size = path.stat().st_size
        h = hashlib.blake2b(digest_size=16)
        with path.open("rb") as f:
            if size <= PARTIAL_HASH_BYTES * 2:
                while True:
                    chunk = f.read(PARTIAL_HASH_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
            else:
                h.update(f.read(PARTIAL_HASH_BYTES))
                f.seek(max(size - PARTIAL_HASH_BYTES, 0))
                h.update(f.read(PARTIAL_HASH_BYTES))
        return h.hexdigest()
    except Exception:
        return None


def full_hash_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.blake2b(digest_size=32)
        with path.open("rb") as f:
            while True:
                chunk = f.read(FULL_HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def suggest_best_keep(sd: FileInfo, drive: FileInfo) -> Tuple[str, str]:
    # Prefer earliest capture time when both are known; otherwise prefer drive.
    if sd.exif_dt is not None and drive.exif_dt is not None and sd.exif_dt != drive.exif_dt:
        if sd.exif_dt < drive.exif_dt:
            return "SD", "Earlier DateTimeOriginal"
        return "DRIVE", "Earlier DateTimeOriginal"
    return "DRIVE", "Already in destination structure"


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
    partial_cache: Dict[Path, Optional[str]] = {}
    full_cache: Dict[Path, Optional[str]] = {}

    def get_partial(path: Path) -> Optional[str]:
        if path not in partial_cache:
            partial_cache[path] = partial_hash_file(path)
        return partial_cache[path]

    def get_full(path: Path) -> Optional[str]:
        if path not in full_cache:
            full_cache[path] = full_hash_file(path)
        return full_cache[path]

    def choose_exact_candidate(sd: FileInfo, candidates: List[FileInfo]) -> Tuple[Optional[FileInfo], str]:
        if not candidates:
            return None, ""
        if len(candidates) == 1:
            return candidates[0], "Exact stem+ext+size match"

        sd_partial = get_partial(sd.path)
        if sd_partial is None:
            return candidates[0], "Exact stem+ext+size match (hash unavailable)"

        partial_hits = [drv for drv in candidates if get_partial(drv.path) == sd_partial]
        if len(partial_hits) == 1:
            return partial_hits[0], "Exact stem+ext+size + partial hash match"
        if not partial_hits:
            return candidates[0], "Exact stem+ext+size match"

        sd_full = get_full(sd.path)
        if sd_full is None:
            return partial_hits[0], "Exact stem+ext+size + partial hash collision"

        full_hits = [drv for drv in partial_hits if get_full(drv.path) == sd_full]
        if full_hits:
            return full_hits[0], "Exact stem+ext+size + full hash match"
        return partial_hits[0], "Exact stem+ext+size + partial hash collision"

    for sd in sd_infos:
        # 1) exact stem+ext
        exact_list = drive_by_stem_ext.get((sd.stem, sd.ext), [])
        exact_same_size = [drv for drv in exact_list if drv.size == sd.size]
        drv_exact, exact_reason = choose_exact_candidate(sd, exact_same_size)
        has_primary_exact = drv_exact is not None
        if drv_exact:
            matches.append(
                MatchRow(
                    kind="EXACT",
                    sd=sd,
                    drive=drv_exact,
                    format_type="",
                    reason=exact_reason,
                )
            )

        # 2) EXIF correlation
        exif_cands = exif_candidates_within_tolerance(sd, drive_by_exif_dt, exif_tolerance_seconds)

        if exif_cands:
            best_same_ext = next((c for c in exif_cands if c[0].ext == sd.ext), None)
            if best_same_ext and not drv_exact:
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
                has_primary_exact = True

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
        if enable_substring_fallback and (not has_primary_exact):
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


def select_folder_native(prompt: str, initial: Optional[Path] = None) -> Optional[Path]:
    """Open OS-native folder picker (supports SMB/network volumes on macOS Finder picker)."""
    if sys.platform == "darwin":
        safe_prompt = prompt.replace('"', '\\"')
        lines = [f'set pickedFolder to choose folder with prompt "{safe_prompt}"']
        if initial is not None and initial.exists():
            safe_init = str(initial).replace('"', '\\"')
            lines = [f'set pickedFolder to choose folder with prompt "{safe_prompt}" default location POSIX file "{safe_init}"']
        lines.append("POSIX path of pickedFolder")
        script = "\n".join(lines)
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        picked = proc.stdout.strip()
        if not picked:
            return None
        p = Path(picked).expanduser()
        return p if p.exists() and p.is_dir() else None

    # Fallback: tkinter native dialog where available.
    try:
        from tkinter import Tk, filedialog  # type: ignore
    except Exception:
        return None
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    start_dir = str(initial) if initial and initial.exists() else None
    picked = filedialog.askdirectory(title=prompt, initialdir=start_dir)
    root.destroy()
    if not picked:
        return None
    p = Path(picked).expanduser()
    return p if p.exists() and p.is_dir() else None


def load_recent_paths(store_path: Path = RECENT_PATHS_FILE) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"drive": [], "sd": []}
    if not store_path.exists():
        return out
    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
    except Exception:
        return out
    if not isinstance(raw, dict):
        return out

    for key in ("drive", "sd"):
        vals = raw.get(key, [])
        cleaned: List[str] = []
        seen: set[str] = set()
        if isinstance(vals, list):
            for v in vals:
                if not isinstance(v, str):
                    continue
                s = v.strip()
                if not s or s in seen:
                    continue
                cleaned.append(s)
                seen.add(s)
        out[key] = cleaned[:MAX_RECENT_PATHS]
    return out


def save_recent_paths(recent: Dict[str, List[str]], store_path: Path = RECENT_PATHS_FILE) -> None:
    payload = {
        "drive": list(recent.get("drive", []))[:MAX_RECENT_PATHS],
        "sd": list(recent.get("sd", []))[:MAX_RECENT_PATHS],
    }
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        return


def remember_recent_path(
    recent: Dict[str, List[str]],
    key: str,
    path: Path,
    *,
    limit: int = MAX_RECENT_PATHS,
) -> bool:
    if key not in ("drive", "sd"):
        return False
    s = str(path.expanduser())
    if not s:
        return False
    existing = [p for p in recent.get(key, []) if isinstance(p, str) and p]
    new_list = [s] + [p for p in existing if p != s]
    new_list = new_list[:limit]
    changed = new_list != existing
    recent[key] = new_list
    return changed


def format_exif_dt(ts: Optional[int]) -> str:
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def format_dimensions(fi: FileInfo) -> str:
    if fi.width and fi.height:
        return f"{fi.width}x{fi.height}"
    return "-"


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024.0
    return f"{int(num_bytes)}B"


def quarantine_root_for(sd_root: Path) -> Path:
    return sd_root / QUARANTINE_DIR_NAME


def transaction_log_for(sd_root: Path) -> Path:
    return sd_root / TX_LOG_NAME


def _ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(1, 100000):
        candidate = path.with_name(f"{stem}.{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Unable to allocate unique destination path.")


def move_to_quarantine(sd_file: Path, sd_root: Path, quarantine_root: Path) -> Tuple[bool, str, Optional[Path]]:
    try:
        src_resolved = sd_file.resolve()
        quarantine_resolved = quarantine_root.resolve()
        if quarantine_resolved in src_resolved.parents:
            return False, f"SKIP already quarantined: {sd_file.name}", None
    except Exception:
        pass

    try:
        rel = sd_file.resolve().relative_to(sd_root.resolve())
    except Exception:
        rel = Path("orphans") / sd_file.name

    dst = _ensure_unique_path(quarantine_root / rel)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        sd_file.rename(dst)
        return True, f"MOVED: {sd_file.name} -> {dst}", dst
    except Exception as e:
        return False, f"ERROR: {sd_file.name}: {e}", None


def append_jsonl_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def load_jsonl_records(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    out.append(parsed)
            except Exception:
                continue
    return out


def find_last_pending_tx(records: List[dict]) -> Tuple[Optional[str], List[dict]]:
    moves_by_tx: Dict[str, List[dict]] = defaultdict(list)
    tx_order: List[str] = []
    undone: set[str] = set()

    for rec in records:
        tx_id = rec.get("tx_id")
        rec_type = rec.get("type")
        if not tx_id or not isinstance(tx_id, str):
            continue
        if rec_type == "move":
            if tx_id not in moves_by_tx:
                tx_order.append(tx_id)
            moves_by_tx[tx_id].append(rec)
        elif rec_type == "undo_complete":
            undone.add(tx_id)

    for tx_id in reversed(tx_order):
        if tx_id in undone:
            continue
        moves = moves_by_tx.get(tx_id, [])
        if moves:
            return tx_id, moves

    return None, []


# -----------------------------
# Preview helpers
# -----------------------------

def _preview_cache_path_for(raw_path: Path, cache_root: Path = PREVIEW_CACHE_DIR) -> Path:
    try:
        st = raw_path.stat()
        signature = f"{raw_path.resolve()}|{int(st.st_mtime)}|{int(st.st_size)}"
    except Exception:
        signature = str(raw_path)
    digest = hashlib.blake2b(signature.encode("utf-8"), digest_size=16).hexdigest()
    return cache_root / f"{digest}.jpg"


def _extract_preview_with_exiftool(raw_path: Path, out_jpg: Path) -> Tuple[bool, str]:
    exe = shutil.which("exiftool")
    if not exe:
        return False, "exiftool unavailable"

    for tag in ("-PreviewImage", "-JpgFromRaw"):
        try:
            proc = subprocess.run(
                [exe, "-b", tag, str(raw_path)],
                capture_output=True,
                check=False,
            )
        except Exception as e:
            return False, f"exiftool failed: {e}"

        if proc.returncode == 0 and proc.stdout:
            try:
                out_jpg.parent.mkdir(parents=True, exist_ok=True)
                out_jpg.write_bytes(proc.stdout)
                return True, f"embedded preview ({tag})"
            except Exception as e:
                return False, f"cannot write preview: {e}"

    return False, "no embedded JPEG preview found"


def _extract_preview_with_rawpy(raw_path: Path, out_jpg: Path) -> Tuple[bool, str]:
    try:
        import rawpy  # type: ignore
    except Exception:
        return False, "rawpy unavailable"

    try:
        from PIL import Image  # type: ignore
    except Exception:
        return False, "Pillow unavailable"

    try:
        with rawpy.imread(str(raw_path)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb).save(out_jpg, format="JPEG", quality=90)
        return True, "decoded with rawpy"
    except Exception as e:
        return False, f"raw decode failed: {e}"


def resolve_preview_image(path: Path) -> Tuple[Optional[Path], str]:
    ext = path.suffix.lower()
    if not path.exists() or not path.is_file():
        return None, "file not found"

    if ext in IMAGE_PREVIEW_EXTENSIONS:
        return path, "direct image preview"

    if ext not in RAW_PREVIEW_EXTENSIONS:
        return None, f"unsupported preview extension: {ext or '(none)'}"

    cached = _preview_cache_path_for(path)
    try:
        if cached.exists() and cached.stat().st_size > 0:
            return cached, "cached RAW preview"
    except Exception:
        pass

    ok, msg = _extract_preview_with_exiftool(path, cached)
    if ok:
        return cached, msg

    ok_rawpy, msg_rawpy = _extract_preview_with_rawpy(path, cached)
    if ok_rawpy:
        return cached, msg_rawpy

    return None, f"{msg}; {msg_rawpy}. install exiftool or rawpy+Pillow for RAW previews"


def render_preview_ascii(path: Path, width: int = PREVIEW_CHARS_WIDTH, height: int = PREVIEW_CHARS_HEIGHT) -> Tuple[Optional[str], str]:
    preview_path, source_note = resolve_preview_image(path)
    if preview_path is None:
        return None, source_note

    exe = shutil.which("chafa")
    if not exe:
        return None, f"{source_note}; chafa unavailable"

    attempts = [
        [exe, "--size", f"{width}x{height}", "--symbols", "ascii", "--colors", "0", str(preview_path)],
        [exe, "--size", f"{width}x{height}", str(preview_path)],
    ]
    last_err = ""

    for cmd in attempts:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as e:
            last_err = str(e)
            continue

        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.rstrip(), source_note
        last_err = (proc.stderr or "").strip() or f"exit {proc.returncode}"

    return None, f"{source_note}; chafa failed ({last_err})"


# -----------------------------
# Textual App
# -----------------------------

class PhotoDupeTUI(App):
    TITLE = "Photo Duplicate Finder (EXIF-aware TUI)"

    CSS = """
    Screen { layout: vertical; }

    #top { height: auto; max-height: 55%; overflow-y: auto; padding: 1; }
    #main { height: 1fr; min-height: 12; }

    #left { width: 38%; height: 1fr; }
    #right { width: 62%; height: 1fr; }

    #paths { height: auto; }
    #toggles { height: auto; margin-top: 1; }
    #actions_top { height: auto; margin-top: 1; }
    #actions_bottom { height: auto; margin-top: 1; }
    #paths Select { width: 1fr; margin-top: 1; }
    #workflow_hint { height: auto; padding: 0 0 1 0; }
    #status { height: auto; padding: 1 0; }
    #summary { height: auto; padding: 0 0 1 0; }

    #matches_table { height: 2fr; min-height: 8; }
    #compare_panel { height: 1fr; min-height: 6; border: round $accent; padding: 1; }
    #preview_panel { height: 1fr; min-height: 8; border: round $accent; padding: 1; overflow-y: auto; }
    SelectionList { height: 1fr; min-height: 6; }

    .row { height: auto; }
    .row Input { width: 1fr; min-width: 16; }
    .row Button { width: auto; min-width: 9; margin-left: 1; }
    #actions_top Button, #actions_bottom Button { width: auto; min-width: 14; }
    """

    all_matches: List[MatchRow] = []
    cross_types: List[str] = []
    enabled_cross_types: set[str] = set()
    last_sd_root: Optional[Path] = None
    pending_confirm_action: Optional[str] = None
    pending_confirm_until: float = 0.0
    recent_paths: Dict[str, List[str]] = {"drive": [], "sd": []}
    recent_events_suspended: bool = False
    preview_generation: int = 0

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="top"):
            with Vertical(id="paths"):
                yield Static("Drive path (destination / archive):")
                with Horizontal(classes="row"):
                    yield Input(placeholder="/Volumes/Photos or D:\\Photos", id="drive_input")
                    yield Button("Pick…", id="pick_drive_btn")
                    yield Button("Native…", id="native_drive_btn")
                yield Select([], prompt="Recent drive paths", allow_blank=True, compact=True, id="drive_recent_select")

                yield Static("SD card path (source):")
                with Horizontal(classes="row"):
                    yield Input(placeholder="/Volumes/SDCARD or E:\\DCIM", id="sd_input")
                    yield Button("Pick…", id="pick_sd_btn")
                    yield Button("Native…", id="native_sd_btn")
                yield Select([], prompt="Recent SD paths", allow_blank=True, compact=True, id="sd_recent_select")

                yield Static("Rename prefix (non-destructive):")
                yield Input(value="COPIED_", id="prefix_input")

            with Horizontal(id="toggles"):
                yield Static("Legacy substring fallback:")
                yield Switch(value=True, id="substring_switch")
                yield Static("EXIF tolerance (seconds):")
                yield Input(value="2", id="tol_input")

            with Horizontal(id="actions_top"):
                yield Button("Scan", id="scan_btn", variant="primary")
                yield Button("Quarantine selected", id="quarantine_btn", variant="warning")
                yield Button("Undo quarantine", id="undo_btn")
                yield Button("View quarantine", id="view_quarantine_btn")

            with Horizontal(id="actions_bottom"):
                yield Button("Rename selected (legacy)", id="apply_btn")
                yield Button("Clear", id="clear_btn")

            yield Static("Workflow: Scan -> review -> Quarantine selected. Rename is legacy.", id="workflow_hint")
            yield Static("", id="status")
            yield Static("", id="summary")

        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Cross-format filters (toggle conversions)")
                yield SelectionList(id="formats_list")

            with Vertical(id="right"):
                yield Static("Matches (review) — includes Reason")
                yield DataTable(id="matches_table")
                yield Static("Compare panel")
                yield Static("Select a match to compare metadata.", id="compare_panel")
                yield Static("Preview panel")
                yield Static("Select a match to render previews.", id="preview_panel")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#matches_table", DataTable)
        table.add_columns(
            "Type",
            "SD File",
            "SD Ext",
            "Drive File",
            "Drive Ext",
            "Conversion",
            "Keep",
            "Reason",
        )
        table.cursor_type = "row"

        if exifread is None:
            self._set_summary("EXIF engine: exifread not installed. Install for best matching: pip install exifread")
        else:
            self._set_summary("EXIF engine: exifread enabled (capture-time matching active).")

        self.recent_paths = load_recent_paths()
        self._refresh_recent_select("drive")
        self._refresh_recent_select("sd")

        self._set_status(
            "Pick folders, Scan, and review matches. Destructive actions require a second press to confirm."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "scan_btn":
            self.action_scan()
        elif bid == "quarantine_btn":
            self.action_quarantine()
        elif bid == "undo_btn":
            self.action_undo_last_apply()
        elif bid == "view_quarantine_btn":
            self.action_view_quarantine()
        elif bid == "apply_btn":
            self.action_apply_rename()
        elif bid == "clear_btn":
            self.action_clear()
        elif bid == "pick_drive_btn":
            self.action_pick_folder("drive_input")
        elif bid == "native_drive_btn":
            self.action_pick_folder_native("drive_input")
        elif bid == "pick_sd_btn":
            self.action_pick_folder("sd_input")
        elif bid == "native_sd_btn":
            self.action_pick_folder_native("sd_input")

    # -----------------
    # Messages
    # -----------------

    def on_folder_picked(self, message: FolderPicked) -> None:
        self._set_input_path(message.field_id, message.path)

    # -----------------
    # Helpers
    # -----------------

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _set_summary(self, msg: str) -> None:
        self.query_one("#summary", Static).update(msg)

    def _set_compare(self, msg: str) -> None:
        self.query_one("#compare_panel", Static).update(msg)

    def _set_preview(self, msg: str) -> None:
        self.query_one("#preview_panel", Static).update(msg)

    def _format_preview_block(self, label: str, path: Path) -> str:
        art, source = render_preview_ascii(path)
        if art:
            return "\n".join(
                [
                    f"{label}: {path.name}",
                    art,
                    f"Source: {source}",
                ]
            )
        return "\n".join(
            [
                f"{label}: {path.name}",
                f"Preview unavailable: {source}",
            ]
        )

    def _format_preview_panel(self, row: MatchRow) -> str:
        sd_block = self._format_preview_block("SD", row.sd.path)
        drive_block = self._format_preview_block("Drive", row.drive.path)
        return "\n\n".join([sd_block, drive_block])

    def _update_preview_if_current(self, generation: int, text: str) -> None:
        if generation != self.preview_generation:
            return
        self._set_preview(text)

    def _preview_worker(self, row: MatchRow, generation: int) -> None:
        rendered = self._format_preview_panel(row)
        self.call_from_thread(self._update_preview_if_current, generation, rendered)

    def _request_preview_render(self, row: Optional[MatchRow]) -> None:
        self.preview_generation += 1
        generation = self.preview_generation

        if row is None:
            self._set_preview("Select a match to render previews.")
            return

        self._set_preview("Rendering previews…")
        self.run_worker(
            lambda: self._preview_worker(row, generation),
            exclusive=False,
            name=f"preview_worker_{generation}",
            thread=True,
        )

    def _refresh_recent_select(self, key: str) -> None:
        select_id = "#drive_recent_select" if key == "drive" else "#sd_recent_select"
        select = self.query_one(select_id, Select)
        options = [(p, p) for p in self.recent_paths.get(key, [])]
        self.recent_events_suspended = True
        try:
            select.set_options(options)
            select.value = Select.NULL
        finally:
            self.recent_events_suspended = False

    def _remember_recent(self, key: str, path: Path) -> None:
        if not path.exists() or not path.is_dir():
            return
        if not remember_recent_path(self.recent_paths, key, path):
            return
        save_recent_paths(self.recent_paths)
        self._refresh_recent_select(key)

    def _clear_pending_confirmation(self) -> None:
        self.pending_confirm_action = None
        self.pending_confirm_until = 0.0

    def _confirm_or_arm(self, action_key: str, action_label: str, count: int) -> bool:
        now = time.monotonic()
        if self.pending_confirm_action == action_key and now <= self.pending_confirm_until:
            self._clear_pending_confirmation()
            return True

        self.pending_confirm_action = action_key
        self.pending_confirm_until = now + CONFIRM_WINDOW_SECONDS
        self._set_status(
            f"Confirm {action_label}: press the same button again within "
            f"{int(CONFIRM_WINDOW_SECONDS)}s ({count} files)."
        )
        return False

    def _get_sd_root_from_input(self) -> Optional[Path]:
        raw = self.query_one("#sd_input", Input).value.strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        if p.exists() and p.is_dir():
            return p
        return None

    def _unique_sd_files(self, rows: List[MatchRow]) -> List[Path]:
        out: List[Path] = []
        seen: set[Path] = set()
        for m in rows:
            if m.sd.path not in seen:
                seen.add(m.sd.path)
                out.append(m.sd.path)
        return out

    def _format_compare_panel(self, row: Optional[MatchRow]) -> str:
        if row is None:
            return "Select a match to compare metadata."

        keep_side, keep_reason = suggest_best_keep(row.sd, row.drive)
        lines = [
            f"Best keep: {keep_side} ({keep_reason})",
            f"Match reason: {row.reason}",
            "",
            f"{'Field':<14} {'SD':<34} {'Drive'}",
            f"{'Name':<14} {row.sd.path.name:<34.34} {row.drive.path.name}",
            f"{'Size':<14} {format_size(row.sd.size):<34} {format_size(row.drive.size)}",
            f"{'Capture':<14} {format_exif_dt(row.sd.exif_dt):<34} {format_exif_dt(row.drive.exif_dt)}",
            f"{'Dimensions':<14} {format_dimensions(row.sd):<34} {format_dimensions(row.drive)}",
            f"{'Camera':<14} {(row.sd.camera_model or '-'): <34.34} {row.drive.camera_model or '-'}",
            f"{'Lens':<14} {(row.sd.lens_model or '-'): <34.34} {row.drive.lens_model or '-'}",
            "",
            f"SD path: {row.sd.path}",
            f"Drive path: {row.drive.path}",
        ]
        return "\n".join(lines)

    def _match_from_row_key(self, row_key) -> Optional[MatchRow]:
        key_val = getattr(row_key, "value", None)
        if key_val is None:
            return None
        try:
            idx = int(str(key_val))
        except Exception:
            return None
        filtered = self._current_filtered_matches()
        if idx < 0 or idx >= len(filtered):
            return None
        return filtered[idx]

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
            keep_side, keep_reason = suggest_best_keep(m.sd, m.drive)
            table.add_row(
                m.kind,
                m.sd.path.name,
                m.sd.ext,
                m.drive.path.name,
                m.drive.ext,
                m.format_type,
                f"{keep_side} ({keep_reason})",
                m.reason,
                key=str(i),
            )

        counts = Counter(m.kind for m in filtered)
        exact_n = counts.get("EXACT", 0)
        cross_n = counts.get("CROSS", 0)
        unique_sd = len(self._unique_sd_files(filtered))
        self._set_status(
            f"Showing {len(filtered)} matches — Exact: {exact_n} | Cross: {cross_n} | "
            f"Unique SD files: {unique_sd} | Conversions enabled: "
            f"{len(self.enabled_cross_types)}/{len(self.cross_types)}"
        )
        first = filtered[0] if filtered else None
        self._set_compare(self._format_compare_panel(first))
        self._request_preview_render(first)

    # -----------------
    # Actions
    # -----------------

    def action_pick_folder(self, field_id: str) -> None:
        self._clear_pending_confirmation()
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

    def _set_input_path(self, field_id: str, path: Path) -> None:
        self.query_one(f"#{field_id}", Input).value = str(path)
        key = "drive" if field_id == "drive_input" else "sd"
        self._remember_recent(key, path)

    def action_pick_folder_native(self, field_id: str) -> None:
        self._clear_pending_confirmation()
        raw = self.query_one(f"#{field_id}", Input).value.strip()
        start = None
        if raw:
            p = Path(raw).expanduser()
            if p.exists() and p.is_dir():
                start = p
            elif p.parent.exists() and p.parent.is_dir():
                start = p.parent

        label = "Drive" if field_id == "drive_input" else "SD card"
        self._set_status(f"Opening native folder picker for {label}…")
        self.run_worker(
            lambda: self._pick_folder_native_worker(field_id, label, start),
            exclusive=False,
            name=f"native_pick_{field_id}",
            thread=True,
        )

    def _pick_folder_native_worker(self, field_id: str, label: str, start: Optional[Path]) -> None:
        picked = select_folder_native(f"Select {label} folder", initial=start)
        if picked is None:
            self.call_from_thread(self._set_status, f"Native picker canceled or unavailable for {label}.")
            return
        self.call_from_thread(self._set_input_path, field_id, picked)
        self.call_from_thread(self._set_status, f"Selected {label}: {picked}")

    def action_clear(self) -> None:
        self._clear_pending_confirmation()
        self.all_matches = []
        self.cross_types = []
        self.enabled_cross_types = set()
        self.last_sd_root = None
        self.query_one("#formats_list", SelectionList).clear_options()
        self.query_one("#matches_table", DataTable).clear()
        self._set_compare("Select a match to compare metadata.")
        self._request_preview_render(None)
        self._set_status("Cleared. Pick folders and Scan.")

    def action_scan(self) -> None:
        self._clear_pending_confirmation()
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

        self._remember_recent("drive", drive_path)
        self._remember_recent("sd", sd_path)
        self.last_sd_root = sd_path
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
        self.enabled_cross_types = {str(value) for value in sl.selected}
        self._refresh_matches_table()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "matches_table":
            return
        row = self._match_from_row_key(event.row_key)
        self._set_compare(self._format_compare_panel(row))
        self._request_preview_render(row)

    def on_select_changed(self, event: Select.Changed) -> None:
        if self.recent_events_suspended:
            return
        sid = event.select.id
        if sid not in {"drive_recent_select", "sd_recent_select"}:
            return
        value = event.value
        if not isinstance(value, str) or not value:
            return

        p = Path(value).expanduser()
        if not p.exists() or not p.is_dir():
            self._set_status(f"Recent path is unavailable: {p}")
            return

        field_id = "drive_input" if sid == "drive_recent_select" else "sd_input"
        self._set_input_path(field_id, p)
        label = "Drive" if field_id == "drive_input" else "SD card"
        self._set_status(f"Loaded recent {label} path: {p}")

    def action_view_quarantine(self) -> None:
        self._clear_pending_confirmation()
        sd_root = self._get_sd_root_from_input() or self.last_sd_root
        if sd_root is None:
            self._set_status("Set a valid SD path to view quarantine.")
            return
        self.push_screen(QuarantineViewer(quarantine_root_for(sd_root)))

    def action_quarantine(self) -> None:
        if not self.all_matches:
            self._set_status("No matches loaded. Scan first.")
            return
        filtered = self._current_filtered_matches()
        if not filtered:
            self._set_status("No matches after filters.")
            return

        sd_root = self._get_sd_root_from_input() or self.last_sd_root
        if sd_root is None or not sd_root.exists():
            self._set_status("Set a valid SD path before quarantining.")
            return

        total = len(self._unique_sd_files(filtered))
        if total == 0:
            self._set_status("No files to quarantine.")
            return
        if not self._confirm_or_arm("quarantine", "quarantine", total):
            return

        self._set_status("Moving selected SD matches to quarantine…")
        self.run_worker(
            lambda: self._quarantine_worker(filtered, sd_root),
            exclusive=True,
            name="quarantine_worker",
            thread=True,
        )

    def _quarantine_worker(self, filtered: List[MatchRow], sd_root: Path) -> None:
        worker = get_current_worker()
        sd_unique = self._unique_sd_files(filtered)
        total = len(sd_unique)
        if total == 0:
            self.call_from_thread(self._set_status, "No files to quarantine.")
            return

        quarantine_root = quarantine_root_for(sd_root)
        tx_log = transaction_log_for(sd_root)
        tx_id = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"

        moved = 0
        skipped = 0
        errors = 0

        for i, sd_file in enumerate(sd_unique, 1):
            if worker.is_cancelled:
                return
            success, msg, dst = move_to_quarantine(sd_file, sd_root, quarantine_root)
            if success and dst is not None:
                moved += 1
                append_jsonl_record(
                    tx_log,
                    {
                        "type": "move",
                        "tx_id": tx_id,
                        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "src": str(sd_file),
                        "dst": str(dst),
                    },
                )
            else:
                if msg.startswith("SKIP"):
                    skipped += 1
                else:
                    errors += 1
            self.call_from_thread(self._set_status, f"{msg} ({i}/{total})")

        self.call_from_thread(
            self._set_status,
            f"Quarantine complete. Moved {moved}/{total}. Skipped {skipped}. Errors {errors}. "
            f"Transaction: {tx_id}",
        )

    def action_undo_last_apply(self) -> None:
        sd_root = self._get_sd_root_from_input() or self.last_sd_root
        if sd_root is None or not sd_root.exists():
            self._set_status("Set a valid SD path before undo.")
            return

        tx_log = transaction_log_for(sd_root)
        records = load_jsonl_records(tx_log)
        tx_id, moves = find_last_pending_tx(records)
        if tx_id is None or not moves:
            self._set_status("No pending quarantine transaction to undo.")
            return

        if not self._confirm_or_arm(f"undo:{tx_id}", "undo last quarantine", len(moves)):
            return

        self._set_status(f"Undoing quarantine transaction {tx_id} ({len(moves)} files)…")
        self.run_worker(
            lambda: self._undo_last_apply_worker(tx_log, tx_id, moves),
            exclusive=True,
            name="undo_worker",
            thread=True,
        )

    def _undo_last_apply_worker(self, tx_log: Path, tx_id: str, moves: List[dict]) -> None:
        worker = get_current_worker()

        restored = 0
        skipped = 0
        errors = 0

        for i, rec in enumerate(reversed(moves), 1):
            if worker.is_cancelled:
                return
            src = Path(str(rec.get("src", "")))
            dst = Path(str(rec.get("dst", "")))
            if not dst.exists():
                skipped += 1
                self.call_from_thread(
                    self._set_status,
                    f"SKIP missing quarantine file: {dst} ({i}/{len(moves)})",
                )
                continue
            if src.exists():
                skipped += 1
                self.call_from_thread(
                    self._set_status,
                    f"SKIP target exists: {src} ({i}/{len(moves)})",
                )
                continue
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                dst.rename(src)
                restored += 1
                self.call_from_thread(
                    self._set_status,
                    f"RESTORED: {src.name} ({i}/{len(moves)})",
                )
            except Exception as e:
                errors += 1
                self.call_from_thread(
                    self._set_status,
                    f"ERROR restoring {src.name}: {e} ({i}/{len(moves)})",
                )

        append_jsonl_record(
            tx_log,
            {
                "type": "undo_complete",
                "tx_id": tx_id,
                "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "restored": restored,
                "skipped": skipped,
                "errors": errors,
            },
        )
        self.call_from_thread(
            self._set_status,
            f"Undo complete for {tx_id}. Restored {restored}. Skipped {skipped}. Errors {errors}.",
        )

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

        total = len(self._unique_sd_files(filtered))
        if not self._confirm_or_arm("rename", "legacy rename", total):
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
        sd_unique = self._unique_sd_files(filtered)

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
