# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Winnow (package name `winnow`) — a Textual TUI app that finds photos on an SD card that already exist on a destination drive, then lets the user review matches and quarantine (or legacy-rename) the SD copies. The active codebase is `photo_dupe.py`. The older `copied.py` and `copied.sh` predecessors are no longer tracked in the repo (kept on disk only, pending relocation).

## Commands

```bash
# Run the TUI app
uv run python photo_dupe.py

# Run all tests
uv run python -m pytest tests/

# Run a single test file
uv run python -m pytest tests/test_photo_dupe_safety.py

# Run a single test
uv run python -m pytest tests/test_photo_dupe_safety.py::SafetyRulesTests::test_paths_overlap_detects_same_and_nested
```

Uses `uv` for dependency management (Python >=3.11). Dependencies: `textual`, `exifread`, `textual-image`, `pillow`, `rawpy`.

## Architecture

Everything lives in a single file: `photo_dupe.py` (~2200 lines). Key sections in order:

1. **Config constants** — extension sets (BLACKLIST, EXIF, RAW, IMAGE preview), hash parameters, quarantine/transaction log names
2. **Data models** — `FileInfo` (frozen dataclass per file with EXIF metadata) and `MatchRow` (a paired SD↔Drive match with kind/reason)
3. **Screens** — `FolderPicker` (modal directory tree picker), `QuarantineViewer` (read-only list of quarantined files)
4. **EXIF helpers** — `read_exif_quick()` extracts DateTimeOriginal, camera model, lens, dimensions via `exifread`
5. **Lazy EXIF loading** — `load_exif_for_fileinfo()` and `load_exif_for_matches()` defer EXIF reading to post-match phase for speed. Module-level `_exif_cache` avoids re-reading.
6. **Scanning** — `scan_directory_parallel_infos()` walks a directory, chunks the file list, and processes in parallel (ThreadPoolExecutor when off main thread, ProcessPoolExecutor with fallback otherwise). EXIF is NOT read during scan — only path/stem/ext/size/mtime are collected.
7. **Matching** — `find_matches_hybrid()` matches SD→Drive by exact stem+ext+size (with partial/full blake2b hash disambiguation), optional substring fallback. `find_content_matches()` catches renamed duplicates via size+hash matching. EXIF-time correlation is intentionally disabled to avoid burst-shot false positives.
8. **Safety** — quarantine moves files to `.photo_dupe_quarantine/` with a JSONL transaction log (`.photo_dupe_transactions.jsonl`), enabling undo. Destructive actions require a second button press within a confirmation window.
9. **Preview pipeline** — resolves preview images (direct for JPG/PNG, exiftool/rawpy extraction for RAW), renders side-by-side via `textual-image` widgets (sixel/iTerm2/kitty with auto-fallback).
10. **TUI (`PhotoDupeTUI`)** — the main Textual `App`. Three-column layout: left (cross-format filters + selection controls), middle (matches DataTable + compare panel), right (side-by-side image preview panel). Workers run on background threads via `run_worker(thread=True)` and post back with `call_from_thread`.

## Tests

Two test files in `tests/`, both using `unittest`:
- `test_photo_dupe_safety.py` — core logic: matching, hashing, scanning, path safety, quarantine transactions, recent paths
- `test_photo_dupe_preview.py` — preview pipeline: cache path stability, exiftool/rawpy extraction, fallback behavior

Tests import directly from `photo_dupe` (no package install needed). Use `tempfile.TemporaryDirectory` for isolation and `unittest.mock.patch` for subprocess/platform mocking.

## Key Design Decisions

- EXIF is deferred to post-match — scanning only collects stat metadata; EXIF is loaded lazily for matched files only, dramatically reducing scan time.
- Matching never uses EXIF timestamps (burst-shot false-positive risk); EXIF data is display/cluster-only.
- Content-based matching (size → partial hash → full hash) catches renamed duplicates that name-based matching misses.
- Clustering groups matches by camera model + capture time proximity for review convenience.
- The scan always uses ThreadPoolExecutor when running from a worker thread (macOS process pool issues).
- Preview uses `textual-image` for side-by-side real image display (sixel/iTerm2/kitty with auto-fallback).
- All file mutations (quarantine, undo, rename) are exclusive workers — only one can run at a time.

## Optional External Tools (for preview)

- `exiftool` — extracts embedded JPEG previews from RAW files
- `rawpy` + `Pillow` — fallback RAW decode if exiftool unavailable
