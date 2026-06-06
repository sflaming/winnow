#!/usr/bin/env python3
"""
Photo Duplicate Finder + TUI Review + Safe Rename (EXIF-aware matching + folder picker)

Adds a modal folder picker so users don't need to type deep paths.

Match order:
  1) Exact stem+ext match
  2) Optional legacy substring fallback (toggle)

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
from typing import Dict, List, Tuple, Optional, Callable, cast
from datetime import datetime

try:
    from textual_image.renderable import Image as _RendImage  # noqa: F401 — early import for terminal detection
    from textual_image.widget import Image as ImageWidget
    HAS_TEXTUAL_IMAGE = True
except ImportError:
    HAS_TEXTUAL_IMAGE = False
    ImageWidget = None

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
    ProgressBar,
)
from textual.reactive import reactive
from textual.worker import get_current_worker
from textual.screen import Screen
from textual.message import Message
from textual.widget import Widget


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
PREVIEW_CMD_TIMEOUT_SECONDS = 4.0
PHOTO_CLUSTER_GAP_SECONDS = 2
NAME_SCAN_FOLDER_FIELD_ID = "__name_scan_folder__"
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
    format_type: str            # ".raf -> .jpg" cross or ".jpg" same-format
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
# Lazy EXIF loading (post-match)
# -----------------------------

_exif_cache: Dict[Path, Tuple] = {}


def clear_exif_cache() -> None:
    _exif_cache.clear()


def load_exif_for_fileinfo(fi: FileInfo) -> FileInfo:
    if fi.ext not in EXIF_CANDIDATE_EXTS:
        return fi
    if fi.path in _exif_cache:
        exif_dt, camera_model, lens_model, width, height = _exif_cache[fi.path]
    else:
        exif_dt, camera_model, lens_model, width, height = read_exif_quick(fi.path)
        _exif_cache[fi.path] = (exif_dt, camera_model, lens_model, width, height)
    return FileInfo(
        path=fi.path,
        stem=fi.stem,
        ext=fi.ext,
        size=fi.size,
        mtime=fi.mtime,
        exif_dt=exif_dt,
        camera_model=camera_model,
        lens_model=lens_model,
        width=width,
        height=height,
    )


def load_exif_for_matches(matches: List[MatchRow]) -> List[MatchRow]:
    out: List[MatchRow] = []
    for m in matches:
        new_sd = load_exif_for_fileinfo(m.sd)
        new_drive = load_exif_for_fileinfo(m.drive)
        if new_sd is m.sd and new_drive is m.drive:
            out.append(m)
        else:
            out.append(MatchRow(
                kind=m.kind,
                sd=new_sd,
                drive=new_drive,
                format_type=m.format_type,
                reason=m.reason,
            ))
    return out


# -----------------------------
# Scanning
# -----------------------------

def collect_all_files(directory: Path) -> List[Path]:
    all_files: List[Path] = []
    for root, dirs, files in os.walk(directory):
        # Skip quarantine and transaction-log directories in-place so
        # os.walk won't descend into them on subsequent iterations.
        dirs[:] = [d for d in dirs if d != QUARANTINE_DIR_NAME]
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

            out.append(
                FileInfo(
                    path=p,
                    stem=stem,
                    ext=ext,
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                )
            )
        except Exception:
            continue

    return out


def scan_directory_parallel_infos(
    directory: Path,
    description: str,
    emit_progress=None,
    progress_callback=None,
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
                if progress_callback:
                    progress_callback(completed, len(chunks))
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


def normalize_sd_stem_for_substring(sd_stem: str, strip_token: str) -> str:
    if not strip_token:
        return sd_stem
    if not sd_stem.startswith(strip_token):
        return sd_stem
    stripped = sd_stem[len(strip_token):]
    # Avoid empty normalized stem, since "" in any string is True.
    if not stripped:
        return sd_stem
    return stripped


def find_matches_hybrid(
    sd_infos: List[FileInfo],
    drive_infos: List[FileInfo],
    *,
    enable_substring_fallback: bool,
    exif_tolerance_seconds: int,
    sd_stem_strip_token: str = "",
) -> Tuple[List[MatchRow], List[str]]:
    # EXIF-time correlation is intentionally disabled due to burst-shot false positives.
    _ = exif_tolerance_seconds
    drive_by_stem_ext, _ = build_drive_indexes(drive_infos)

    drive_by_ext_for_sub: Dict[str, List[Tuple[str, FileInfo]]] = defaultdict(list)
    if enable_substring_fallback:
        for fi in drive_infos:
            drive_by_ext_for_sub[fi.ext].append((fi.stem, fi))

    matches: List[MatchRow] = []
    format_types: set[str] = set()
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
            format_types.add(sd.ext)
            matches.append(
                MatchRow(
                    kind="EXACT",
                    sd=sd,
                    drive=drv_exact,
                    format_type=sd.ext,
                    reason=exact_reason,
                )
            )

        # 2) substring fallback (legacy) only if no exact match
        if enable_substring_fallback and (not has_primary_exact):
            sd_stem = normalize_sd_stem_for_substring(sd.stem, sd_stem_strip_token)
            sd_ext = sd.ext

            # exact-ext substring
            for drive_stem, drv in drive_by_ext_for_sub.get(sd_ext, []):
                if sd_stem in drive_stem:
                    format_types.add(sd_ext)
                    matches.append(
                        MatchRow(
                            kind="EXACT",
                            sd=sd,
                            drive=drv,
                            format_type=sd_ext,
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
                        format_types.add(fmt)
                        matches.append(
                            MatchRow(
                                kind="CROSS",
                                sd=sd,
                                drive=drv,
                                format_type=fmt,
                                reason="Substring stem match (legacy)",
                            )
                        )

    return matches, sorted(format_types)


def build_drive_size_index(drive_infos: List[FileInfo]) -> Dict[int, List[FileInfo]]:
    by_size: Dict[int, List[FileInfo]] = defaultdict(list)
    for fi in drive_infos:
        if fi.size > 0:
            by_size[fi.size].append(fi)
    return dict(by_size)


def find_content_matches(
    sd_infos: List[FileInfo],
    drive_infos: List[FileInfo],
    already_matched_sd_paths: set[Path],
) -> List[MatchRow]:
    drive_by_size = build_drive_size_index(drive_infos)
    matches: List[MatchRow] = []
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

    for sd in sd_infos:
        if sd.path in already_matched_sd_paths:
            continue
        if sd.size == 0:
            continue

        candidates = drive_by_size.get(sd.size)
        if not candidates:
            continue

        sd_partial = get_partial(sd.path)
        if sd_partial is None:
            continue

        partial_hits = [drv for drv in candidates if get_partial(drv.path) == sd_partial]
        if not partial_hits:
            continue

        if len(partial_hits) == 1:
            drv = partial_hits[0]
            fmt = sd.ext if sd.ext == drv.ext else f"{sd.ext} -> {drv.ext}"
            matches.append(MatchRow(
                kind="EXACT",
                sd=sd,
                drive=drv,
                format_type=fmt,
                reason="Content hash match (different filename)",
            ))
            continue

        sd_full = get_full(sd.path)
        if sd_full is None:
            drv = partial_hits[0]
            fmt = sd.ext if sd.ext == drv.ext else f"{sd.ext} -> {drv.ext}"
            matches.append(MatchRow(
                kind="EXACT",
                sd=sd,
                drive=drv,
                format_type=fmt,
                reason="Content hash match (different filename, hash unavailable)",
            ))
            continue

        full_hits = [drv for drv in partial_hits if get_full(drv.path) == sd_full]
        if full_hits:
            drv = full_hits[0]
            fmt = sd.ext if sd.ext == drv.ext else f"{sd.ext} -> {drv.ext}"
            matches.append(MatchRow(
                kind="EXACT",
                sd=sd,
                drive=drv,
                format_type=fmt,
                reason="Content hash match (different filename)",
            ))

    return matches


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


def rename_file_replace_substring(sd_file: Path, needle: str, replacement: str) -> Tuple[bool, str, Optional[Path]]:
    stem = sd_file.stem
    if needle not in stem:
        return False, f"SKIP no match: {sd_file.name}", None

    new_stem = stem.replace(needle, replacement)
    if new_stem == stem:
        return False, f"SKIP unchanged: {sd_file.name}", None

    new_name = sd_file.with_name(f"{new_stem}{sd_file.suffix}")
    if new_name.exists():
        return False, f"SKIP exists: {new_name.name}", None

    try:
        sd_file.rename(new_name)
        return True, f"RENAMED: {sd_file.name} -> {new_name.name}", new_name
    except Exception as e:
        return False, f"ERROR: {sd_file.name}: {e}", None


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
                timeout=PREVIEW_CMD_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, "exiftool timed out"
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


# -----------------------------
# Textual App
# -----------------------------

class PhotoDupeTUI(App):
    TITLE = "Winnow — Photo Duplicate Finder (EXIF-aware TUI)"

    CSS = """
    Screen { layout: vertical; }

    #toolbar { height: auto; max-height: 14; padding: 0 1; overflow-y: auto; }
    #paths_row { height: auto; }
    #paths_row .path_col { width: 1fr; height: auto; padding-right: 1; }
    #paths_row .path_col Input { width: 1fr; }
    #paths_row .path_col Select { width: 1fr; }
    #paths_row #btn_col { width: auto; height: auto; min-width: 18; }
    #paths_row #btn_col Button { width: 100%; }
    #options_row { height: auto; margin-top: 1; }
    #rename_row { height: auto; margin-top: 1; }
    #rename_tools_row { height: auto; margin-top: 1; }
    #prefix_input { width: 16; }
    #options_row Button { width: auto; min-width: 10; }
    #rename_find_input { width: 18; }
    #rename_replace_input { width: 18; }
    #options_row Static, #rename_row Static, #rename_tools_row Static { width: auto; padding: 0 1 0 0; }
    #rename_row Button, #rename_tools_row Button { width: auto; min-width: 10; }
    #status { height: auto; }
    #progress_bar { height: 1; display: none; }
    #progress_bar.visible { display: block; }

    #main { height: 1fr; min-height: 0; }
    #table_area { height: 1fr; min-height: 6; }
    #matches_table { height: 1fr; }
    #detail_area { height: 1fr; min-height: 6; }
    #compare_panel { width: 1fr; height: 1fr; border: round $accent; padding: 1; overflow-y: auto; }
    #preview_panel { width: 1fr; height: 1fr; border: round $accent; padding: 0; overflow: hidden; }
    .preview-placeholder { width: 1fr; height: 1fr; padding: 1; content-align: center middle; }
    .preview-side { width: 1fr; height: 1fr; }
    .preview-image { width: 1fr; height: 1fr; }
    .preview-label { height: auto; text-align: center; }

    #formats_popup { height: auto; max-height: 10; display: none; }
    #formats_popup.visible { display: block; }
    #formats_popup SelectionList { height: auto; max-height: 8; }
    """

    BINDINGS = [
        ("s", "scan", "Scan"),
        ("space", "toggle_selected_row", "Toggle"),
        ("ctrl+a", "select_all", "Sel all"),
        ("ctrl+n", "select_none", "Sel none"),
        ("f", "toggle_formats", "Filters"),
        ("r", "apply_rename", "Rename"),
        ("q", "quarantine", "Quarantine"),
        ("u", "undo_last_apply", "Undo"),
        ("c", "clear", "Clear"),
    ]

    all_matches: List[MatchRow] = []
    format_types: List[str] = []
    enabled_format_types: set[str] = set()
    last_sd_root: Optional[Path] = None
    pending_confirm_action: Optional[str] = None
    pending_confirm_until: float = 0.0
    recent_paths: Dict[str, List[str]] = {"drive": [], "sd": []}
    recent_events_suspended: bool = False
    preview_focus_row: Optional[MatchRow] = None
    selected_match_keys: set[str] = set()
    cluster_by_match_key: Dict[str, str] = {}
    name_scan_folder: Optional[Path] = None
    name_scan_needle: str = ""
    name_scan_targets: List[Path] = []
    pending_name_scan_needle: str = ""
    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="toolbar"):
            with Horizontal(id="paths_row"):
                with Vertical(classes="path_col"):
                    yield Static("Drive:")
                    yield Input(placeholder="/Volumes/Photos or D:\\Photos", id="drive_input")
                    yield Select([], prompt="Recent drives", allow_blank=True, compact=True, id="drive_recent_select")
                with Vertical(classes="path_col"):
                    yield Static("SD:")
                    yield Input(placeholder="/Volumes/SDCARD or E:\\DCIM", id="sd_input")
                    yield Select([], prompt="Recent SD", allow_blank=True, compact=True, id="sd_recent_select")
                with Vertical(id="btn_col"):
                    yield Button("Pick drive", id="pick_drive_btn")
                    yield Button("Pick SD", id="pick_sd_btn")
                    yield Button("Scan", id="scan_btn", variant="primary")
            with Horizontal(id="options_row"):
                yield Static("Substring:")
                yield Switch(value=True, id="substring_switch")
                yield Static("Content match:")
                yield Switch(value=True, id="content_match_switch")
                yield Button("Filters", id="filters_btn")
                yield Button("Clear", id="clear_btn")
            with Horizontal(id="rename_row"):
                yield Static("Prefix:")
                yield Input(value="COPIED_", id="prefix_input")
                yield Button("Rename", id="rename_btn", variant="success")
                yield Button("Quarantine", id="quarantine_btn", variant="warning")
                yield Button("Undo", id="undo_btn")
                yield Button("View Q", id="view_quarantine_btn")
            with Horizontal(id="rename_tools_row"):
                yield Static("Find in name / scan strip:")
                yield Input(placeholder="COPIED_", id="rename_find_input")
                yield Static("Replace with:")
                yield Input(placeholder="ARCHIVED_", id="rename_replace_input")
                yield Button("Scan names", id="rename_scan_names_btn")
                yield Button("Replace text", id="rename_replace_btn")
                yield Button("Remove text", id="rename_remove_btn")

        yield Static("", id="status")
        yield ProgressBar(total=100, id="progress_bar")

        with Vertical(id="main"):
            with Vertical(id="formats_popup"):
                yield Static("Format filters:")
                yield SelectionList(id="formats_list")
            with Vertical(id="table_area"):
                yield DataTable(id="matches_table")
            with Horizontal(id="detail_area"):
                yield Static("Select a match to compare.", id="compare_panel")
                with Horizontal(id="preview_panel"):
                    yield Static("Select a match to preview.", classes="preview-placeholder")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#matches_table", DataTable)
        table.add_columns("Sel", "SD File", "Drive File", "Type", "Cluster", "Reason")
        table.cursor_type = "row"

        if exifread is None:
            self._set_status("EXIF engine: exifread not installed. EXIF metadata/clusters unavailable.")
        else:
            self._set_status("EXIF engine: exifread enabled (metadata + clustering; not used for matching).")

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
        elif bid == "rename_btn":
            self.action_apply_rename()
        elif bid == "quarantine_btn":
            self.action_quarantine()
        elif bid == "undo_btn":
            self.action_undo_last_apply()
        elif bid == "view_quarantine_btn":
            self.action_view_quarantine()
        elif bid == "clear_btn":
            self.action_clear()
        elif bid == "rename_replace_btn":
            self.action_replace_name_substring()
        elif bid == "rename_remove_btn":
            self.action_remove_name_substring()
        elif bid == "rename_scan_names_btn":
            self.action_scan_names_for_substring()
        elif bid == "pick_drive_btn":
            self.action_pick_folder("drive_input")
        elif bid == "pick_sd_btn":
            self.action_pick_folder("sd_input")
        elif bid == "filters_btn":
            self.action_toggle_formats()

    # -----------------
    # Messages
    # -----------------

    def on_folder_picked(self, message: FolderPicked) -> None:
        if message.field_id == NAME_SCAN_FOLDER_FIELD_ID:
            needle = self.pending_name_scan_needle or self.query_one("#rename_find_input", Input).value.strip()
            self.pending_name_scan_needle = ""
            self._start_name_substring_scan(message.path, needle)
            return
        self._set_input_path(message.field_id, message.path)

    # -----------------
    # Helpers
    # -----------------

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _set_compare(self, msg: str) -> None:
        self.query_one("#compare_panel", Static).update(msg)

    def _update_preview_images(self, row: Optional[MatchRow]) -> None:
        panel = self.query_one("#preview_panel")
        panel.remove_children()
        image_widget_factory = cast(Callable[..., Widget], ImageWidget) if ImageWidget is not None else None

        if row is None or not HAS_TEXTUAL_IMAGE or image_widget_factory is None:
            msg = "Select a match to preview."
            if row is not None:
                msg = "textual-image not available. Install with: pip install textual-image[textual]"
            panel.mount(Static(msg, classes="preview-placeholder"))
            return

        panel.mount(Static("Loading previews\u2026", classes="preview-placeholder"))

        captured_row = row

        def do_preview(image_widget_factory: Callable[..., Widget] = image_widget_factory) -> None:
            try:
                sd_img, sd_note = resolve_preview_image(captured_row.sd.path)
                drv_img, drv_note = resolve_preview_image(captured_row.drive.path)
            except FileNotFoundError:
                return

            def mount_images() -> None:
                # File may have been quarantined/renamed between resolve and mount.
                if sd_img is not None and not sd_img.exists():
                    panel.remove_children()
                    panel.mount(Static("Preview unavailable: SD file was moved or renamed.", classes="preview-placeholder"))
                    return
                if drv_img is not None and not drv_img.exists():
                    panel.remove_children()
                    panel.mount(Static("Preview unavailable: Drive file was moved or renamed.", classes="preview-placeholder"))
                    return

                panel.remove_children()

                sd_container = Vertical(classes="preview-side")
                drv_container = Vertical(classes="preview-side")
                panel.mount(sd_container)
                panel.mount(drv_container)

                sd_container.mount(Static(f"SD: {captured_row.sd.path.name}", classes="preview-label"))
                if sd_img is not None:
                    try:
                        sd_container.mount(image_widget_factory(sd_img, classes="preview-image"))
                    except Exception as e:
                        sd_container.mount(Static(f"No preview: {e}", classes="preview-image"))
                else:
                    sd_container.mount(Static(f"No preview: {sd_note}", classes="preview-image"))

                drv_container.mount(Static(f"Drive: {captured_row.drive.path.name}", classes="preview-label"))
                if drv_img is not None:
                    try:
                        drv_container.mount(image_widget_factory(drv_img, classes="preview-image"))
                    except Exception as e:
                        drv_container.mount(Static(f"No preview: {e}", classes="preview-image"))
                else:
                    drv_container.mount(Static(f"No preview: {drv_note}", classes="preview-image"))

            self.call_from_thread(mount_images)

        self.run_worker(
            do_preview,
            group="preview",
            exclusive=True,
            name="preview_worker",
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

    def _fileinfo_with_new_path(self, fi: FileInfo, new_path: Path) -> FileInfo:
        return FileInfo(
            path=new_path,
            stem=new_path.stem,
            ext=new_path.suffix.lower(),
            size=fi.size,
            mtime=fi.mtime,
            exif_dt=fi.exif_dt,
            camera_model=fi.camera_model,
            lens_model=fi.lens_model,
            width=fi.width,
            height=fi.height,
        )

    def _apply_path_rename_map(self, rename_map: Dict[Path, Path]) -> None:
        if not rename_map:
            return

        old_matches = list(self.all_matches)
        old_selected = set(self.selected_match_keys)
        old_focus_key = self._match_key(self.preview_focus_row) if self.preview_focus_row else None

        new_matches: List[MatchRow] = []
        new_selected: set[str] = set()
        new_focus: Optional[MatchRow] = None

        for old_m in old_matches:
            old_key = self._match_key(old_m)
            sd = self._fileinfo_with_new_path(old_m.sd, rename_map[old_m.sd.path]) if old_m.sd.path in rename_map else old_m.sd
            drv = self._fileinfo_with_new_path(old_m.drive, rename_map[old_m.drive.path]) if old_m.drive.path in rename_map else old_m.drive
            new_m = MatchRow(
                kind=old_m.kind,
                sd=sd,
                drive=drv,
                format_type=old_m.format_type,
                reason=old_m.reason,
            )
            new_matches.append(new_m)
            new_key = self._match_key(new_m)
            if old_key in old_selected:
                new_selected.add(new_key)
            if old_focus_key and old_key == old_focus_key:
                new_focus = new_m

        self.all_matches = new_matches
        self.selected_match_keys = new_selected
        self.preview_focus_row = new_focus
        self._rebuild_clusters()

    def _iter_sd_files(self, sd_root: Path) -> List[Path]:
        out: List[Path] = []
        for root, dirs, files in os.walk(sd_root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            root_path = Path(root)
            for name in files:
                if name.startswith("."):
                    continue
                p = root_path / name
                if p.is_file():
                    out.append(p)
        return out

    def _files_with_substring_in_name(self, root: Path, needle: str) -> List[Path]:
        needle_s = needle.strip()
        if not needle_s:
            return []
        out: List[Path] = []
        for p in self._iter_sd_files(root):
            if needle_s in p.name:
                out.append(p)
        out.sort(key=lambda p: str(p).lower())
        return out

    def _default_name_scan_start(self) -> Path:
        start = self.name_scan_folder or self._get_sd_root_from_input() or self.last_sd_root
        if start is not None and start.exists() and start.is_dir():
            return start
        if Path.home().exists():
            return Path.home()
        return Path("C:\\") if os.name == "nt" else Path("/")

    def _start_name_substring_scan(self, folder: Path, needle: str) -> None:
        needle_s = needle.strip()
        if not needle_s:
            self._set_status("Find text is required.")
            return
        self._set_status(f"Scanning folder for '{needle_s}' in filenames: {folder}")
        self.run_worker(
            lambda: self._scan_name_substring_worker(folder, needle_s),
            exclusive=False,
            name="scan_name_substring_worker",
            thread=True,
        )

    def _scan_name_substring_worker(self, folder: Path, needle: str) -> None:
        worker = get_current_worker()
        targets = self._files_with_substring_in_name(folder, needle)
        if worker.is_cancelled:
            return

        def done() -> None:
            self.name_scan_folder = folder
            self.name_scan_needle = needle
            self.name_scan_targets = targets
            if not targets:
                self._set_status(f"No filenames in {folder} contain '{needle}'.")
                return
            self._set_status(
                f"Found {len(targets)} filenames containing '{needle}' in {folder}. "
                "Use Replace text or Remove text."
            )

        self.call_from_thread(done)

    def _rename_targets_from_last_scan(self, needle: str) -> Optional[List[Path]]:
        if not needle:
            self._set_status("Find text is required.")
            return None
        if self.name_scan_folder is None:
            self._set_status("Use 'Scan names' first to pick a folder and find filenames.")
            return None
        if self.name_scan_needle != needle:
            self._set_status("Find text changed. Run 'Scan names' again for the current text.")
            return None

        if not self.name_scan_targets:
            self._set_status("Last 'Scan names' found no matches. Scan again with a different folder or text.")
            return None

        targets = [p for p in self.name_scan_targets if p.exists() and p.is_file()]
        if not targets:
            self._set_status("Scanned targets are no longer available. Run 'Scan names' again.")
            return None
        return targets

    def _format_compare_panel(self, row: Optional[MatchRow]) -> str:
        if row is None:
            return "Select a match to compare metadata."

        keep_side, keep_reason = suggest_best_keep(row.sd, row.drive)
        cluster = self._cluster_label_for(row)
        lines = [
            f"Best keep: {keep_side} ({keep_reason})",
            f"Match reason: {row.reason}",
            f"Cluster: {cluster}",
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

    def _match_key(self, m: MatchRow) -> str:
        return "|".join(
            [
                m.kind,
                str(m.sd.path),
                str(m.drive.path),
                m.format_type,
            ]
        )

    def _is_selected(self, m: MatchRow) -> bool:
        return self._match_key(m) in self.selected_match_keys

    def _cluster_label_for(self, m: MatchRow) -> str:
        return self.cluster_by_match_key.get(self._match_key(m), "-")

    def _rebuild_clusters(self) -> None:
        self.cluster_by_match_key = {}

        with_dt = [m for m in self.all_matches if m.sd.exif_dt is not None]
        if not with_dt:
            return

        # Group bursts by camera model and close capture times.
        with_dt.sort(
            key=lambda m: (
                (m.sd.camera_model or "").strip().lower(),
                int(m.sd.exif_dt or 0),
                str(m.sd.path),
            )
        )

        cluster_index = 0
        last_camera = ""
        last_dt: Optional[int] = None
        for m in with_dt:
            camera = (m.sd.camera_model or "").strip().lower()
            dt = int(m.sd.exif_dt or 0)
            is_new_cluster = (
                cluster_index == 0
                or camera != last_camera
                or last_dt is None
                or (dt - last_dt) > PHOTO_CLUSTER_GAP_SECONDS
            )
            if is_new_cluster:
                cluster_index += 1
            self.cluster_by_match_key[self._match_key(m)] = f"C{cluster_index:03d}"
            last_camera = camera
            last_dt = dt

    def _set_selected(self, m: MatchRow, selected: bool) -> None:
        k = self._match_key(m)
        if selected:
            self.selected_match_keys.add(k)
        else:
            self.selected_match_keys.discard(k)

    def _selected_filtered_matches(self) -> List[MatchRow]:
        return [m for m in self._current_filtered_matches() if self._is_selected(m)]

    def _select_all_filtered(self) -> int:
        filtered = self._current_filtered_matches()
        for m in filtered:
            self._set_selected(m, True)
        return len(filtered)

    def _select_none_filtered(self) -> int:
        filtered = self._current_filtered_matches()
        for m in filtered:
            self._set_selected(m, False)
        return len(filtered)

    @property
    def _content_match_enabled(self) -> bool:
        try:
            return self.query_one("#content_match_switch", Switch).value
        except Exception:
            return True

    def _read_inputs(self) -> Tuple[Optional[Path], Optional[Path], str, bool]:
        drive = self.query_one("#drive_input", Input).value.strip()
        sd = self.query_one("#sd_input", Input).value.strip()
        prefix = self.query_one("#prefix_input", Input).value.strip()

        substring = self.query_one("#substring_switch", Switch).value

        if not drive or not sd:
            return None, None, prefix, substring

        return Path(drive).expanduser(), Path(sd).expanduser(), prefix, substring

    def _refresh_formats_list(self) -> None:
        sl = self.query_one("#formats_list", SelectionList)
        sl.clear_options()
        if not self.format_types:
            return

        if not self.enabled_format_types:
            self.enabled_format_types = set(self.format_types)

        sl.add_options([(fmt, fmt, (fmt in self.enabled_format_types)) for fmt in self.format_types])

    def _current_filtered_matches(self) -> List[MatchRow]:
        out: List[MatchRow] = []
        for m in self.all_matches:
            if m.format_type in self.enabled_format_types:
                out.append(m)
        return out

    def _refresh_matches_table(self) -> None:
        table = self.query_one("#matches_table", DataTable)
        table.clear()

        filtered = self._current_filtered_matches()
        focus_key = self._match_key(self.preview_focus_row) if self.preview_focus_row else None
        for i, m in enumerate(filtered):
            table.add_row(
                "[x]" if self._is_selected(m) else "[ ]",
                m.sd.path.name,
                m.drive.path.name,
                m.kind,
                self._cluster_label_for(m),
                m.reason,
                key=str(i),
            )

        counts = Counter(m.kind for m in filtered)
        exact_n = counts.get("EXACT", 0)
        cross_n = counts.get("CROSS", 0)
        unique_sd = len(self._unique_sd_files(filtered))
        selected_filtered = self._selected_filtered_matches()
        selected_rows = len(selected_filtered)
        selected_unique_sd = len(self._unique_sd_files(selected_filtered))
        visible_clusters = {
            self._cluster_label_for(m)
            for m in filtered
            if self._cluster_label_for(m) != "-"
        }
        self._set_status(
            f"Showing {len(filtered)} matches — Exact: {exact_n} | Cross: {cross_n} | "
            f"Unique SD files: {unique_sd} | Selected rows: {selected_rows} "
            f"(unique SD {selected_unique_sd}) | Clusters: {len(visible_clusters)} | Formats enabled: "
            f"{len(self.enabled_format_types)}/{len(self.format_types)}"
        )
        focus = None
        if focus_key is not None:
            focus = next((m for m in filtered if self._match_key(m) == focus_key), None)
        if focus is None:
            focus = filtered[0] if filtered else None
        self.preview_focus_row = focus
        self._set_compare(self._format_compare_panel(focus))
        self._update_preview_images(focus)

    # -----------------
    # Actions
    # -----------------

    def action_pick_folder(self, field_id: str) -> None:
        self._clear_pending_confirmation()
        if sys.platform == "darwin":
            self.action_pick_folder_native(field_id)
            return
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

    def action_scan_names_for_substring(self) -> None:
        self._clear_pending_confirmation()
        needle = self.query_one("#rename_find_input", Input).value.strip()
        if not needle:
            self._set_status("Find text is required.")
            return

        start = self._default_name_scan_start()
        self.pending_name_scan_needle = needle

        if sys.platform == "darwin":
            self._set_status("Opening native folder picker for filename text scan…")
            self.run_worker(
                lambda: self._pick_name_scan_folder_native_worker(needle, start),
                exclusive=False,
                name="name_scan_pick_native_worker",
                thread=True,
            )
            return

        self.push_screen(FolderPicker(field_id=NAME_SCAN_FOLDER_FIELD_ID, start_path=start))

    def _pick_name_scan_folder_native_worker(self, needle: str, start: Path) -> None:
        picked = select_folder_native("Select folder to scan filenames", initial=start)
        if picked is None:
            self.pending_name_scan_needle = ""
            self.call_from_thread(self._set_status, "Native folder picker canceled for filename text scan.")
            return
        self.pending_name_scan_needle = ""
        self.call_from_thread(self._start_name_substring_scan, picked, needle)

    def action_clear(self) -> None:
        self._clear_pending_confirmation()
        self.all_matches = []
        self.format_types = []
        self.enabled_format_types = set()
        self.selected_match_keys.clear()
        self.cluster_by_match_key = {}
        self.last_sd_root = None
        self.preview_focus_row = None
        self.name_scan_folder = None
        self.name_scan_needle = ""
        self.name_scan_targets = []
        self.pending_name_scan_needle = ""
        self.query_one("#formats_popup").remove_class("visible")
        self.query_one("#formats_list", SelectionList).clear_options()
        self.query_one("#matches_table", DataTable).clear()
        self._set_compare("Select a match to compare.")
        self._update_preview_images(None)
        self._set_status("Cleared. Pick folders and Scan.")

    def action_scan(self) -> None:
        self._clear_pending_confirmation()
        drive_path, sd_path, _, substring = self._read_inputs()
        scan_strip_token = self.query_one("#rename_find_input", Input).value.strip()
        scan_strip_desc = (
            f"scan strip token='{scan_strip_token}'" if scan_strip_token else "scan strip token=off"
        )
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
        self._set_status(f"Starting scan… ({scan_strip_desc})")
        self.run_worker(
            lambda: self._scan_worker(drive_path, sd_path, substring, scan_strip_token),
            exclusive=True,
            name="scan_worker",
            thread=True,
        )

    def _show_progress(self, progress: float) -> None:
        bar = self.query_one("#progress_bar", ProgressBar)
        bar.update(progress=progress)

    def _set_progress_visible(self, visible: bool) -> None:
        bar = self.query_one("#progress_bar")
        if visible:
            bar.add_class("visible")
            self._show_progress(0)
        else:
            bar.remove_class("visible")

    def _scan_worker(
        self,
        drive_path: Path,
        sd_path: Path,
        substring: bool,
        scan_strip_token: str,
    ) -> None:
        worker = get_current_worker()
        scan_strip_desc = (
            f"scan strip token='{scan_strip_token}'" if scan_strip_token else "scan strip token=off"
        )

        # Phase weights: drive 40%, SD 40%, name match 8%, content match 7%, EXIF 5%
        clear_exif_cache()

        def emit(msg: str) -> None:
            if worker.is_cancelled:
                return
            self.call_from_thread(self._set_status, msg)

        def make_chunk_cb(offset: float, weight: float):
            def cb(completed: int, total: int) -> None:
                if worker.is_cancelled:
                    return
                pct = offset + weight * (completed / max(total, 1))
                self.call_from_thread(self._show_progress, pct * 100)
            return cb

        self.call_from_thread(self._set_progress_visible, True)

        emit(f"Scanning drive: {drive_path}")
        drive_infos = scan_directory_parallel_infos(
            drive_path, "files on drive",
            emit_progress=emit,
            progress_callback=make_chunk_cb(0.0, 0.40),
        )
        if worker.is_cancelled:
            return

        emit(f"Scanning SD card: {sd_path}")
        sd_infos = scan_directory_parallel_infos(
            sd_path, "files on SD card",
            emit_progress=emit,
            progress_callback=make_chunk_cb(0.40, 0.40),
        )
        if worker.is_cancelled:
            return

        if not sd_infos:
            self.call_from_thread(self._set_status, "No files found on SD card.")
            self.call_from_thread(self._set_progress_visible, False)
            return
        if not drive_infos:
            self.call_from_thread(self._set_status, "No files found on drive.")
            self.call_from_thread(self._set_progress_visible, False)
            return

        emit("Matching (name-based)…")
        self.call_from_thread(self._show_progress, 80)
        matches, format_types = find_matches_hybrid(
            sd_infos,
            drive_infos,
            enable_substring_fallback=substring,
            exif_tolerance_seconds=0,
            sd_stem_strip_token=scan_strip_token,
        )

        if worker.is_cancelled:
            return

        # Content-based matching for unmatched SD files
        content_match_enabled = self._content_match_enabled
        if content_match_enabled:
            emit("Matching (content-based)…")
            self.call_from_thread(self._show_progress, 88)
            matched_sd_paths = {m.sd.path for m in matches}
            content_matches = find_content_matches(sd_infos, drive_infos, matched_sd_paths)
            matches.extend(content_matches)
            format_types_set = set(format_types)
            for cm in content_matches:
                if cm.format_type:
                    format_types_set.add(cm.format_type)
            format_types = sorted(format_types_set)

        if worker.is_cancelled:
            return

        # Load EXIF for matched files only (much faster than scanning all)
        emit("Loading EXIF metadata for matches…")
        self.call_from_thread(self._show_progress, 95)
        matches = load_exif_for_matches(matches)

        def done() -> None:
            self._show_progress(100)
            self.all_matches = matches
            self.format_types = format_types
            self.enabled_format_types = set(format_types)
            self.selected_match_keys = {self._match_key(m) for m in matches}
            self._rebuild_clusters()
            self._refresh_formats_list()
            self._refresh_matches_table()
            self._set_progress_visible(False)

            if not matches:
                self._set_status(
                    f"No matches found. substring fallback={'on' if substring else 'off'}; "
                    f"{scan_strip_desc}."
                )
                return

            exact_total = sum(1 for m in matches if m.kind == "EXACT")
            cross_total = sum(1 for m in matches if m.kind == "CROSS")
            self._set_status(
                f"Scan complete — matches: {len(matches)} (Exact {exact_total}, Cross {cross_total}). "
                f"substring fallback={'on' if substring else 'off'}; {scan_strip_desc}."
            )

        self.call_from_thread(done)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if event.selection_list.id != "formats_list":
            return
        sl = self.query_one("#formats_list", SelectionList)
        if sl.option_count == 0:
            return
        self.enabled_format_types = {str(value) for value in sl.selected}
        self._refresh_matches_table()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "matches_table":
            return
        row = self._match_from_row_key(event.row_key)
        self.preview_focus_row = row
        self._set_compare(self._format_compare_panel(row))
        self._update_preview_images(row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "matches_table":
            return
        row = self._match_from_row_key(event.row_key)
        if row is None:
            return
        self._set_selected(row, not self._is_selected(row))
        self._refresh_matches_table()

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

    def action_toggle_formats(self) -> None:
        self.query_one("#formats_popup").toggle_class("visible")

    def action_select_all(self) -> None:
        count = self._select_all_filtered()
        self._refresh_matches_table()
        self._set_status(f"Selected all visible rows ({count}).")

    def action_select_none(self) -> None:
        count = self._select_none_filtered()
        self._refresh_matches_table()
        self._set_status(f"Cleared selection for visible rows ({count}).")

    def action_toggle_selected_row(self) -> None:
        table = self.query_one("#matches_table", DataTable)
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            row = self._match_from_row_key(cell_key.row_key)
        except Exception:
            row = None
        if row is None:
            self._set_status("No row highlighted to toggle.")
            return
        self._set_selected(row, not self._is_selected(row))
        self._refresh_matches_table()

    def action_quarantine(self) -> None:
        if not self.all_matches:
            self._set_status("No matches loaded. Scan first.")
            return
        selected = self._selected_filtered_matches()
        if not selected:
            self._set_status("No selected rows after filters.")
            return

        sd_root = self._get_sd_root_from_input() or self.last_sd_root
        if sd_root is None or not sd_root.exists():
            self._set_status("Set a valid SD path before quarantining.")
            return

        total = len(self._unique_sd_files(selected))
        if total == 0:
            self._set_status("No files to quarantine.")
            return
        if not self._confirm_or_arm("quarantine", "quarantine", total):
            return

        self._set_status("Moving selected SD matches to quarantine…")
        self.run_worker(
            lambda: self._quarantine_worker(selected, sd_root),
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
        moved_sd_paths: set[Path] = set()

        for i, sd_file in enumerate(sd_unique, 1):
            if worker.is_cancelled:
                return
            success, msg, dst = move_to_quarantine(sd_file, sd_root, quarantine_root)
            if success and dst is not None:
                moved += 1
                moved_sd_paths.add(sd_file)
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

        status = (
            f"Quarantine complete. Moved {moved}/{total}. Skipped {skipped}. Errors {errors}. "
            f"Transaction: {tx_id}"
        )

        def finish_quarantine() -> None:
            if moved_sd_paths:
                self.all_matches = [
                    m for m in self.all_matches if m.sd.path not in moved_sd_paths
                ]
                remaining_keys = {self._match_key(m) for m in self.all_matches}
                self.selected_match_keys &= remaining_keys
                self._rebuild_clusters()
                self._refresh_matches_table()
            self._set_status(status)

        self.call_from_thread(finish_quarantine)

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

        prefix = self.query_one("#prefix_input", Input).value.strip()
        prefix_err = validate_prefix(prefix)
        if prefix_err:
            self._set_status(prefix_err)
            return
        selected = self._selected_filtered_matches()
        if not selected:
            self._set_status("No selected rows after filters.")
            return

        total = len(self._unique_sd_files(selected))
        if not self._confirm_or_arm("rename", "legacy rename", total):
            return

        self._set_status(f"Applying rename prefix '{prefix}'…")
        self.run_worker(
            lambda: self._apply_worker(selected, prefix),
            exclusive=True,
            name="apply_worker",
            thread=True,
        )

    def action_replace_name_substring(self) -> None:
        needle = self.query_one("#rename_find_input", Input).value.strip()
        replacement = self.query_one("#rename_replace_input", Input).value
        targets = self._rename_targets_from_last_scan(needle)
        if targets is None:
            return

        if not self._confirm_or_arm("rename_replace", "replace filename text", len(targets)):
            return

        self._set_status(f"Replacing '{needle}' -> '{replacement}' in {len(targets)} filenames…")
        self.run_worker(
            lambda: self._rename_substring_worker(targets, needle, replacement, "replace"),
            exclusive=True,
            name="rename_replace_worker",
            thread=True,
        )

    def action_remove_name_substring(self) -> None:
        needle = self.query_one("#rename_find_input", Input).value.strip()
        targets = self._rename_targets_from_last_scan(needle)
        if targets is None:
            return

        if not self._confirm_or_arm("rename_remove", "remove filename text", len(targets)):
            return

        self._set_status(f"Removing '{needle}' from {len(targets)} filenames…")
        self.run_worker(
            lambda: self._rename_substring_worker(targets, needle, "", "remove"),
            exclusive=True,
            name="rename_remove_worker",
            thread=True,
        )

    def _rename_substring_worker(
        self,
        targets: List[Path],
        needle: str,
        replacement: str,
        mode: str,
    ) -> None:
        worker = get_current_worker()
        total = len(targets)
        ok = 0
        skipped = 0
        errors = 0
        rename_map: Dict[Path, Path] = {}

        for i, p in enumerate(targets, 1):
            if worker.is_cancelled:
                return
            success, msg, dst = rename_file_replace_substring(p, needle, replacement)
            if success and dst is not None:
                ok += 1
                rename_map[p] = dst
            else:
                if msg.startswith("SKIP"):
                    skipped += 1
                else:
                    errors += 1
            self.call_from_thread(self._set_status, f"{msg} ({i}/{total})")

        status = (
            f"Name {mode} complete. Renamed {ok}/{total}. Skipped {skipped}. Errors {errors}."
        )

        def finish_name_rename() -> None:
            if rename_map:
                self._apply_path_rename_map(rename_map)
                self._refresh_matches_table()
            self._set_status(status)

        self.call_from_thread(finish_name_rename)

    def _apply_worker(self, filtered: List[MatchRow], prefix: str) -> None:
        worker = get_current_worker()

        # Rename each SD file once
        sd_unique = self._unique_sd_files(filtered)

        total = len(sd_unique)
        ok = 0
        skipped = 0
        errors = 0
        rename_map: Dict[Path, Path] = {}

        for i, sd_file in enumerate(sd_unique, 1):
            if worker.is_cancelled:
                return
            success, msg = rename_file_with_prefix(sd_file, prefix)
            if success:
                ok += 1
                rename_map[sd_file] = sd_file.parent / f"{prefix}{sd_file.name}"
            else:
                if msg.startswith("SKIP"):
                    skipped += 1
                else:
                    errors += 1
            self.call_from_thread(self._set_status, f"{msg} ({i}/{total})")

        status = (
            f"Rename complete. Renamed {ok}/{total}. Skipped {skipped}. Errors {errors}."
        )

        def finish_rename() -> None:
            if rename_map:
                self._apply_path_rename_map(rename_map)
                self._refresh_matches_table()
            self._set_status(status)

        self.call_from_thread(finish_rename)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Photo Duplicate Finder TUI (EXIF-aware + picker)")
    _ = parser.parse_args(argv)
    PhotoDupeTUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
