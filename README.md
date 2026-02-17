# Photo Duplicate Finder and Renamer

Two versions available: Shell (v1.0) and Python (v2.0 - **RECOMMENDED**)

## Overview

Quickly identify and rename photos on an SD card that already exist on a specified drive, ensuring duplicates are clearly marked for easy organization and cleanup.

## Version Comparison

### Shell Version (copied.sh)
- **Language**: zsh
- **Speed**: Baseline
- **Dependencies**: Native macOS/zsh
- **Best for**: Small datasets (< 1000 files)

### Python Version (copied.py) ⭐ RECOMMENDED
- **Language**: Python 3
- **Speed**: **5-10x faster** than shell version
- **Dependencies**: Python 3 (pre-installed on macOS)
- **Best for**: Any size dataset
- **Matching**: Substring matching (like shell version)
- **Debug Mode**: `--verbose` flag for troubleshooting

## Performance Improvements in Python Version

1. **Parallel Directory Scanning**: Scans both directories simultaneously using multiprocessing
2. **Hash-based Lookups**: O(1) dictionary lookups instead of O(n×m) nested loops
3. **Chunked Processing**: Distributes file processing across CPU cores
4. **Optimized Data Structures**: Uses Python dictionaries instead of shell arrays
5. **Progress Indicators**: Real-time feedback during long operations

### Expected Performance

| Files on Drive | Files on SD Card | Shell Script | Python Script |
|----------------|------------------|--------------|---------------|
| 1,000          | 500              | ~30 sec      | ~5 sec        |
| 10,000         | 1,000            | ~10 min      | ~1 min        |
| 50,000         | 2,000            | ~1 hour      | ~5 min        |

*Actual times vary based on hardware and directory depth*

## Usage

### Python Version (Recommended)

**Normal Mode:**
```bash
./copied.py
```

Or:

```bash
python3 copied.py
```

**Verbose/Debug Mode:**
```bash
./copied.py --verbose
# or
./copied.py -v
```

Verbose mode shows detailed matching information:
- Files grouped by extension
- Each matching attempt with results
- Exact substring matches found
- Summary of matches vs. non-matches

This is helpful for troubleshooting when files aren't matching as expected.

**Help:**
```bash
./copied.py --help
```

### Shell Version

```bash
./copied.sh
```

## How It Works

1. **Select Drive**: Choose the main drive where photos are stored
2. **Select SD Card**: Choose the SD card directory to check
3. **Enter Prefix**: Specify prefix for renamed files (e.g., "COPIED_")
4. **Scan**: Script scans both locations and builds file index
5. **Match**: Finds files with identical basenames and extensions
6. **Review**: Shows all matches for confirmation
7. **Rename**: Adds prefix to matched files on SD card

## Example

**Before:**
```
SD Card/
  ├── IMG_1234.jpg
  ├── IMG_5678.RAF
  └── IMG_9999.jpg

Drive/
  └── Photos/
      ├── IMG_1234.jpg
      └── IMG_5678.RAF
```

**After** (with prefix "COPIED_"):
```
SD Card/
  ├── COPIED_IMG_1234.jpg
  ├── COPIED_IMG_5678.RAF
  └── IMG_9999.jpg
```

## Features

- ✅ **GUI Folder Selection**: User-friendly dialog boxes
- ✅ **Recursive Search**: Searches all subdirectories
- ✅ **Extension Matching**: Matches files with same extension
- ✅ **Cross-Format Detection**: NEW! Detects when same photo exists in different formats (e.g., RAW + JPG)
- ✅ **Format Blacklist**: NEW! Auto-excludes irrelevant formats (.cop, .cos, .cot, etc.) - easily customizable
- ✅ **Interactive Format Filter**: Select which format conversions to include (e.g., include .raf→.jpg, skip others)
- ✅ **Grouped Results**: Separately displays exact matches vs. cross-format matches
- ✅ **Selective Renaming**: Choose to rename exact matches, cross-format matches, or both
- ✅ **Progress Indicators**: Real-time processing updates
- ✅ **Safe Renaming**: Checks for existing files before renaming
- ✅ **Error Handling**: Graceful handling of permission issues
- ✅ **Batch Processing**: Handles thousands of files efficiently
- ✅ **Verbose Mode**: Detailed debugging output with `--verbose` flag

## Requirements

### Python Version
- Python 3.6 or higher (pre-installed on macOS 10.15+)
- tkinter (included with Python on macOS)

### Shell Version
- zsh (default shell on macOS)
- macOS with AppleScript support

## Technical Details

### Python Optimization Techniques

1. **Multiprocessing**: Uses `ProcessPoolExecutor` to leverage multiple CPU cores
2. **Chunked Processing**: Divides file lists into optimal chunks for parallel processing
3. **Dictionary Indexing**: O(1) lookup time vs O(n) for shell arrays
4. **Path Objects**: Uses `pathlib.Path` for efficient file operations
5. **Type Hints**: Ensures code correctness and maintainability

### Algorithm Complexity

**Shell Version:**
- Time Complexity: O(n × m) where n = SD card files, m = drive files
- Space Complexity: O(n + m)

**Python Version:**
- Time Complexity: O(n + m) with parallel processing
- Space Complexity: O(n + m)
- Parallel Speedup: Up to 8x depending on CPU cores

## Troubleshooting

### Permission Errors
Ensure you have read/write access to the SD card and sufficient permissions.

### No Matches Found
- Verify the drive path contains the copied files
- Check that file extensions match exactly
- Ensure files have the same base name

### Python Import Errors
All required modules (os, sys, pathlib, tkinter) are part of Python's standard library on macOS.

## Future Enhancements

Potential improvements:
- [ ] Add checksum-based duplicate detection (byte-for-byte comparison)
- [ ] Support for undo/rollback operations
- [ ] Export match report to CSV
- [ ] Command-line argument support
- [ ] Dry-run mode to preview changes

## License

Free to use and modify for personal or commercial purposes.
