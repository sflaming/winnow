# Preview Setup Guide

Photo Duplicate Finder renders side-by-side image previews of matched
files using `textual-image`. When supported, the terminal displays real
images (via sixel, iTerm2 inline images, or kitty graphics protocol);
otherwise it falls back automatically.

## How it works

When you highlight a match row, the preview panel shows the SD image on
the left and the Drive image on the right. Standard image formats
(JPEG, PNG, etc.) are displayed directly. RAW files require an
extraction step first (see below).

## Required dependencies

### textual-image (installed automatically)

`textual-image` is listed in `pyproject.toml` and installed by
`uv sync`. It handles terminal capability detection and image rendering.

### exiftool (recommended for RAW files)

Many RAW formats (`.raf`, `.cr2`, `.cr3`, `.nef`, `.arw`, `.dng`, ...)
embed a full-size JPEG preview. `exiftool` extracts it for display.
This is fast and produces high-quality previews.

```bash
# macOS
brew install exiftool

# Ubuntu / Debian
sudo apt install libimage-exiftool-perl

# Fedora
sudo dnf install perl-Image-ExifTool
```

Verify: `exiftool -ver`

### rawpy + Pillow (optional fallback for RAW)

If `exiftool` is not available, the app falls back to decoding RAW files
with `rawpy` and converting to JPEG with `Pillow`. This is slower and
requires Python packages (already in `pyproject.toml`):

```bash
uv pip install rawpy Pillow
```

Most users should prefer `exiftool` instead — it's faster and doesn't
require compiling native extensions.

## What works without extra tools

| File type                         | Preview available | + exiftool | + rawpy/Pillow |
|-----------------------------------|-------------------|------------|----------------|
| JPEG (`.jpg`, `.jpeg`)            | Yes               | Yes        | Yes            |
| PNG, GIF, BMP, TIFF, WebP        | Yes               | Yes        | Yes            |
| RAW (`.raf`, `.cr2`, `.nef`, ...) | No                | Yes        | Yes (slower)   |

## Using previews in the TUI

1. **Scan** first (`s` key or Scan button) to populate the matches table.
2. **Highlight** a row in the matches table (arrow keys).
3. The preview panel automatically shows both images side by side.

### Keybindings reference

| Key       | Action                          |
|-----------|---------------------------------|
| `s`       | Start scan                      |
| `space`   | Toggle row selection            |
| `ctrl+a`  | Select all rows                 |
| `ctrl+n`  | Deselect all rows               |
| `f`       | Toggle cross-format filters     |
| `q`       | Quarantine selected files       |
| `u`       | Undo last quarantine            |
| `c`       | Clear all matches               |

## Troubleshooting

### Preview panel shows "textual-image not available"

Run `uv sync` to install the dependency. If you installed manually,
ensure `textual-image[textual]>=0.8.5` is installed in your environment.

### "no embedded JPEG preview found" (RAW files)

The RAW file doesn't contain an embedded preview, or `exiftool` couldn't
extract it. Try installing `rawpy` + `Pillow` as a fallback decoder.

### "exiftool unavailable" + "rawpy unavailable"

Neither RAW preview backend is installed. Install at least one:

```bash
# Preferred
brew install exiftool

# Alternative
uv pip install rawpy Pillow
```

### Blank or garbled preview

`textual-image` auto-detects your terminal's image protocol. For best
results use a terminal with sixel or inline image support:

- **iTerm2** (macOS) — native inline images
- **WezTerm** — sixel support
- **kitty** — kitty graphics protocol
- **foot** — sixel support

If your terminal doesn't support any image protocol, `textual-image`
falls back to a Unicode block-character approximation.

## Cached previews

Extracted RAW previews are cached in `/tmp/winnow_preview_cache/`.
The cache key includes the file's path, modification time, and size, so
it auto-invalidates when files change.
