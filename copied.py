#!/usr/bin/env python3
"""
Photo Duplicate Finder and Renamer
v2.1 - Python optimized version

Quickly identify and rename photos on an SD card that already exist on a 
specified drive, ensuring duplicates are clearly marked for easy organization.
"""

import os
import sys
import argparse
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from tkinter import Tk, filedialog
from typing import Dict, List, Tuple, Set

# Global verbose flag
VERBOSE = False


def select_folder(prompt: str) -> Path:
    """Use GUI to select a folder."""
    root = Tk()
    root.withdraw()  # Hide the main window
    root.attributes('-topmost', True)  # Bring dialog to front
    folder_path = filedialog.askdirectory(title=prompt)
    root.destroy()
    
    if not folder_path:
        print("No folder selected. Operation canceled.")
        sys.exit(1)
    
    return Path(folder_path)


def scan_directory_chunk(args: Tuple[Path, List[Path]]) -> Dict[str, List[Path]]:
    """
    Scan a chunk of directories and build a dictionary of files.
    Returns dict mapping (stem, extension) -> [file_paths]
    """
    root_dir, paths = args
    file_dict = defaultdict(list)
    
    for path in paths:
        if path.is_file():
            stem = path.stem  # filename without extension
            ext = path.suffix.lower()  # extension with dot
            key = (stem, ext)
            file_dict[key].append(path)
    
    return file_dict


def vprint(*args, **kwargs):
    """Print only if verbose mode is enabled."""
    if VERBOSE:
        print(*args, **kwargs)


def scan_directory_parallel(directory: Path, description: str = "files") -> Dict[Tuple[str, str], List[Path]]:
    """
    Scan directory using multiple processes for faster performance.
    Returns a dictionary mapping (basename, extension) to list of file paths.
    """
    print(f"Scanning {directory}...")
    
    # Collect all files first
    all_files = []
    for root, dirs, files in os.walk(directory):
        root_path = Path(root)
        for file in files:
            if not file.startswith('.'):  # Skip hidden files
                all_files.append(root_path / file)
    
    if not all_files:
        return {}
    
    print(f"Found {len(all_files)} {description}. Processing...")
    
    # Split files into chunks for parallel processing
    num_processes = min(os.cpu_count() or 4, 8)  # Use up to 8 cores
    chunk_size = max(len(all_files) // (num_processes * 4), 100)
    chunks = [all_files[i:i + chunk_size] for i in range(0, len(all_files), chunk_size)]
    
    # Process chunks in parallel
    file_dict = defaultdict(list)
    with ProcessPoolExecutor(max_workers=num_processes) as executor:
        futures = [executor.submit(scan_directory_chunk, (directory, chunk)) for chunk in chunks]
        
        completed = 0
        for future in as_completed(futures):
            chunk_dict = future.result()
            # Merge results
            for key, paths in chunk_dict.items():
                file_dict[key].extend(paths)
            completed += 1
            print(f"\rProgress: {completed}/{len(chunks)} chunks processed", end='', flush=True)
    
    print()  # New line after progress
    return dict(file_dict)


def find_matches(sd_files: Dict[Tuple[str, str], List[Path]], 
                 drive_files: Dict[Tuple[str, str], List[Path]]) -> List[Tuple[Path, Path]]:
    """
    Find matching files between SD card and drive using substring matching.
    Matches when SD card file's basename (without extension) is contained in 
    drive file's basename, and they have the same extension.
    
    Returns list of tuples: (sd_file_path, drive_file_path)
    """
    matches = []
    
    print(f"Comparing {len(sd_files)} unique SD card files against drive...")
    vprint("\nVERBOSE MODE: Detailed matching process")
    vprint("=" * 70)
    
    # Build extension-based index for faster lookups
    drive_by_ext = defaultdict(list)
    for (stem, ext), paths in drive_files.items():
        for path in paths:
            drive_by_ext[ext].append((stem, path))
    
    vprint(f"\nDrive files grouped by extension:")
    for ext, files in drive_by_ext.items():
        vprint(f"  {ext}: {len(files)} files")
    
    matched_count = 0
    no_match_count = 0
    
    # For each SD card file, find all drive files with matching substring
    for (sd_stem, sd_ext), sd_paths in sd_files.items():
        vprint(f"\n--- Checking SD card file stem: '{sd_stem}' (ext: {sd_ext}) ---")
        
        # Get all drive files with same extension
        candidate_files = drive_by_ext.get(sd_ext, [])
        vprint(f"  Candidates with extension {sd_ext}: {len(candidate_files)}")
        
        # Find drive files where SD card basename is substring of drive basename
        found_matches = []
        for drive_stem, drive_path in candidate_files:
            if sd_stem in drive_stem:  # Substring match (like shell script does)
                found_matches.append((drive_stem, drive_path))
                vprint(f"  ✓ MATCH: '{sd_stem}' found in '{drive_stem}'")
        
        if found_matches:
            # Create matches for each SD card file with this stem
            for sd_path in sd_paths:
                # Use first matching drive file
                matches.append((sd_path, found_matches[0][1]))
                matched_count += 1
                vprint(f"  → Added match: {sd_path.name} -> {found_matches[0][1].name}")
        else:
            no_match_count += 1
            vprint(f"  ✗ NO MATCH for '{sd_stem}' with extension {sd_ext}")
    
    vprint("\n" + "=" * 70)
    vprint(f"SUMMARY: {matched_count} matches found, {no_match_count} SD files with no match")
    vprint("=" * 70 + "\n")
    
    return matches


def prompt_user(message: str) -> bool:
    """Prompt user for yes/no confirmation."""
    while True:
        response = input(f"{message} (y/n): ").lower().strip()
        if response == 'y':
            return True
        elif response == 'n':
            return False
        else:
            print("Please enter 'y' or 'n'")


def rename_files(matches: List[Tuple[Path, Path]], prefix: str) -> int:
    """
    Rename SD card files with the given prefix.
    Returns number of successfully renamed files.
    """
    renamed_count = 0
    
    for sd_file, _ in matches:
        new_name = sd_file.parent / f"{prefix}{sd_file.name}"
        
        # Handle case where target file already exists
        if new_name.exists():
            print(f"Skipping (already exists): {new_name}")
            continue
        
        try:
            sd_file.rename(new_name)
            print(f"Renamed: {sd_file.name} -> {new_name.name}")
            renamed_count += 1
        except Exception as e:
            print(f"Error renaming {sd_file}: {e}")
    
    return renamed_count


def main(argv=None):
    """Main execution flow."""
    global VERBOSE
    
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='Find and rename duplicate photos on SD card that exist on a drive',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s              # Run with GUI (interactive mode)
  %(prog)s --verbose    # Run with verbose debugging output
  %(prog)s -v           # Short form of verbose
        """
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output for debugging'
    )
    
    args = parser.parse_args(argv)
    VERBOSE = args.verbose
    
    version = "v2.1" if VERBOSE else "v2.0"
    print("=" * 60)
    print(f"Photo Duplicate Finder and Renamer {version}")
    if VERBOSE:
        print("VERBOSE MODE ENABLED")
    print("=" * 60)
    print()
    
    # Step 1: Select drive to search
    print("Step 1: Select the drive to search for copied files.")
    drive_path = select_folder("Select the drive to search for copied files")
    print(f"Selected drive: {drive_path}")
    print()
    
    # Step 2: Select SD card directory
    print("Step 2: Select the SD card directory.")
    sd_card_path = select_folder("Select the SD card directory")
    print(f"Selected SD card: {sd_card_path}")
    print()
    
    # Step 3: Get rename prefix
    rename_prefix = input("Step 3: Enter the prefix for already copied files (e.g., 'COPIED_'): ").strip()
    if not rename_prefix:
        rename_prefix = "COPIED_"
        print(f"Using default prefix: {rename_prefix}")
    print()
    
    # Step 4: Scan both directories in parallel
    print("Step 4: Scanning directories (this may take a moment)...")
    print()
    
    # Use ProcessPoolExecutor to scan both directories simultaneously
    with ProcessPoolExecutor(max_workers=2) as executor:
        drive_future = executor.submit(scan_directory_parallel, drive_path, "files on drive")
        sd_future = executor.submit(scan_directory_parallel, sd_card_path, "files on SD card")
        
        drive_files = drive_future.result()
        sd_files = sd_future.result()
    
    if not sd_files:
        print("No files found on the SD card.")
        return
    
    if not drive_files:
        print("No files found on the drive.")
        return
    
    print()
    
    # Step 5: Find matches
    print("Step 5: Finding matching files...")
    matches = find_matches(sd_files, drive_files)
    
    if not matches:
        print("No matching files found on the selected drive.")
        return
    
    print(f"Found {len(matches)} matching files.")
    print()
    
    # Step 6: Display matches
    print("The following files have been identified as already copied:")
    print("-" * 60)
    print("SD Card File -> Drive File")
    print("-" * 60)
    
    for sd_file, drive_file in matches[:20]:  # Show first 20 matches
        print(f"{sd_file.name} -> {drive_file}")
    
    if len(matches) > 20:
        print(f"... and {len(matches) - 20} more matches")
    
    print("-" * 60)
    print()
    
    # Step 7: Confirm and rename
    if not prompt_user(f"Rename {len(matches)} matched files on the SD card with prefix '{rename_prefix}'?"):
        print("Operation canceled.")
        return
    
    print()
    print("Renaming files...")
    renamed_count = rename_files(matches, rename_prefix)
    
    print()
    print("=" * 60)
    print(f"Operation complete! Renamed {renamed_count} of {len(matches)} files.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nOperation canceled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nError: {e}")
        sys.exit(1)
