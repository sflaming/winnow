# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Photo Duplicate Finder — a Textual TUI app that finds photos on an SD card that already exist on a destination drive, then lets the user review matches and quarantine (or legacy-rename) the SD copies. The active codebase is `photo_dupe.py`; the older `copied.py` and `copied.sh` are legacy predecessors.

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

Uses `uv` for dependency management (Python 3.13). Dependencies: `textual`, `exifread`.

## Architecture

Everything lives in a single file: `photo_dupe.py`. Key sections in order:

1. **Config constants** — extension sets (BLACKLIST, EXIF, RAW, IMAGE preview), hash parameters, preview settings, quarantine/transaction log names
2. **Data models** — `FileInfo` (frozen dataclass per file with EXIF metadata) and `MatchRow` (a paired SD↔Drive match with kind/reason)
3. **Screens** — `FolderPicker` (modal directory tree picker), `QuarantineViewer` (read-only list of quarantined files)
4. **EXIF helpers** — `read_exif_quick()` extracts DateTimeOriginal, camera model, lens, dimensions via `exifread`
5. **Scanning** — `scan_directory_parallel_infos()` walks a directory, chunks the file list, and processes in parallel (ThreadPoolExecutor when off main thread, ProcessPoolExecutor with fallback otherwise)
6. **Matching** — `find_matches_hybrid()` matches SD→Drive by exact stem+ext+size (with partial/full blake2b hash disambiguation), optional substring fallback, and optional cross-format detection. EXIF-time correlation is intentionally disabled to avoid burst-shot false positives.
7. **Safety** — quarantine moves files to `.photo_dupe_quarantine/` with a JSONL transaction log (`.photo_dupe_transactions.jsonl`), enabling undo. Destructive actions require a second button press within a confirmation window.
8. **Preview pipeline** — resolves preview images (direct for JPG/PNG, exiftool/rawpy extraction for RAW), renders ASCII art via `chafa`, with debounced async workers, generation tracking, and an LRU block cache.
9. **TUI (`PhotoDupeTUI`)** — the main Textual `App`. Three-column layout: left (cross-format filters + selection controls), middle (matches DataTable + compare panel), right (ASCII preview panel). Workers run on background threads via `run_worker(thread=True)` and post back with `call_from_thread`.

## Key Design Decisions

- Matching never uses EXIF timestamps (burst-shot false-positive risk); EXIF data is display/cluster-only.
- Clustering groups matches by camera model + capture time proximity for review convenience.
- The scan always uses ThreadPoolExecutor when running from a worker thread (macOS process pool issues).
- Preview rendering is debounced and generation-gated to avoid stale updates overwriting current ones.
- All file mutations (quarantine, undo, rename) are exclusive workers — only one can run at a time.

## Optional External Tools (for preview)

- `exiftool` — extracts embedded JPEG previews from RAW files
- `rawpy` + `Pillow` — fallback RAW decode if exiftool unavailable
- `chafa` — renders images as ASCII art in the terminal preview panel
