# Winnow

A [Textual](https://textual.textualize.io/) terminal UI for clearing your SD card with confidence.

Winnow scans an SD card (or any source folder) against a destination drive, finds the photos that have **already been copied over**, and lets you review each match side by side before safely quarantining the redundant copies on the card. Nothing is deleted — duplicates are moved to a quarantine folder and every action is logged so it can be undone.

## Features

- **Reliable duplicate detection** — matches by name + size with partial/full BLAKE2b hashing to confirm, plus content-only matching to catch files that were renamed after copying.
- **EXIF-aware review** — capture time, camera model, lens, and dimensions are shown for each match. EXIF is loaded only for matched files, so scanning stays fast.
- **Side-by-side previews** — real image previews rendered in the terminal (sixel / iTerm2 / kitty, with automatic fallback), including embedded previews extracted from RAW files.
- **Safe by design** — duplicates are moved to a `.winnow_quarantine/` folder with a JSONL transaction log, so every quarantine and rename can be undone. Destructive actions require a confirming second press.
- **Name-substring scan & rename** — find and rewrite a substring across filenames (e.g. strip an import prefix) in bulk.

## Requirements

- Python ≥ 3.11
- [`uv`](https://docs.astral.sh/uv/) for dependency management

Optional, improves RAW preview quality:
- `exiftool` — extracts embedded JPEG previews from RAW files
- `rawpy` + `Pillow` — fallback RAW decode (installed as dependencies)

## Usage

```bash
# Run the app
uv run python winnow.py
```

Point it at your **destination drive** (where photos already live) and your **SD card / source folder**. Winnow scans both, presents the matches, and lets you review and quarantine the copies that are safely backed up.

## Development

```bash
# Run the full test suite
uv run python -m pytest tests/

# A single test file
uv run python -m pytest tests/test_winnow_safety.py
```

The application lives in a single module, `winnow.py`. See `CLAUDE.md` for an architecture overview and `PREVIEW.md` for details on the preview pipeline.
