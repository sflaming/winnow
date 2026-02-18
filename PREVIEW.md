# Preview Setup Guide

Photo Duplicate Finder can render ASCII art previews of matched images
directly in the terminal. This document covers what you need installed and
how to use the feature.

## Required tools

### chafa (required for all previews)

`chafa` converts images to ASCII/Unicode art for terminal display. Without
it **no previews will render** — you'll see "chafa unavailable" in the
preview panel.

```bash
# macOS
brew install chafa

# Ubuntu / Debian
sudo apt install chafa

# Fedora
sudo dnf install chafa

# Arch
sudo pacman -S chafa
```

Verify: `chafa --version`

### exiftool (recommended for RAW files)

Many RAW formats (`.raf`, `.cr2`, `.cr3`, `.nef`, `.arw`, `.dng`, …)
embed a full-size JPEG preview. `exiftool` extracts it so `chafa` can
render it. This is fast and produces high-quality previews.

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
requires Python packages:

```bash
uv pip install rawpy Pillow
```

Most users should prefer `exiftool` instead — it's faster and doesn't
require compiling native extensions.

## What works without extra tools

| File type                         | chafa only | + exiftool | + rawpy/Pillow |
|-----------------------------------|------------|------------|----------------|
| JPEG (`.jpg`, `.jpeg`)            | Yes        | Yes        | Yes            |
| PNG, GIF, BMP, TIFF, WebP        | Yes        | Yes        | Yes            |
| RAW (`.raf`, `.cr2`, `.nef`, …)   | No         | Yes        | Yes (slower)   |

Standard image formats (JPEG, PNG, etc.) are passed directly to `chafa`
and need no extraction step. RAW files require either `exiftool` or
`rawpy`+`Pillow` to extract/decode a viewable image first.

## Using previews in the TUI

1. **Scan** first (`s` key or Scan button) to populate the matches table.
2. **Highlight** a row in the matches table (arrow keys).
3. **Render preview** with the `p` key. The preview panel (right side of
   the detail area) will show ASCII art for both the SD and Drive copies.
4. **Auto-preview**: toggle the "Auto preview" switch in the options row
   to render previews automatically as you navigate rows.

### Keybindings reference

| Key       | Action                          |
|-----------|---------------------------------|
| `s`       | Start scan                      |
| `p`       | Render preview for selected row |
| `space`   | Toggle row selection            |
| `ctrl+a`  | Select all rows                 |
| `ctrl+n`  | Deselect all rows               |
| `f`       | Toggle cross-format filters     |
| `q`       | Quarantine selected files       |
| `u`       | Undo last quarantine            |
| `c`       | Clear all matches               |

## Troubleshooting

### "chafa unavailable"

`chafa` is not on your `PATH`. Install it (see above) and restart the app.

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

### Preview panel shows text but no art

If you see the label and "Preview unavailable: …" messages, check the
specific error after the colon. Common causes:

- **"chafa failed (timeout)"** — the image is very large or the disk is
  slow. The default timeout is 4 seconds per image.
- **"unsupported preview extension"** — the file type isn't recognized as
  an image or RAW format.
- **"file not found"** — the file was moved or deleted since the scan.

### Preview is cut off or too small

The preview renders at 48 characters wide by 14 lines tall by default.
Make sure your terminal is at least 80 columns wide. The detail area
splits 50/50 between the compare and preview panels — at 120 columns,
each panel gets about 60 characters of width.

## Cached previews

Extracted RAW previews are cached in `/tmp/photo_dupe_preview_cache/`.
The cache key includes the file's path, modification time, and size, so
it auto-invalidates when files change. Rendered ASCII blocks are cached
in memory (up to 128 entries) for fast re-display when navigating back
to previously viewed rows.
