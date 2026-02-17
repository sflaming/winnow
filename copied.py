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

# Blacklist of file extensions to exclude from cross-format matching
# These extensions will never be shown as cross-format matches
# Add or remove extensions as needed (use lowercase, include the dot)
BLACKLIST_EXTENSIONS = {
    '.cop',   # Capture One Process file
    '.cos',   # Capture One Settings file  
    '.cot',   # Capture One Catalog file
    '.xmp',   # Adobe XMP sidecar files
    '.aae',   # Apple Photos adjustment data
    '.thm',   # Thumbnail files
    '.db',    # Database files
    '.ini',   # Configuration files
}


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
                 drive_files: Dict[Tuple[str, str], List[Path]]) -> Tuple[List[Tuple[Path, Path]], List[Tuple[Path, Path, str]]]:
    """
    Find matching files between SD card and drive using substring matching.
    Matches when SD card file's basename (without extension) is contained in 
    drive file's basename.
    
    Returns two lists:
    1. exact_matches: [(sd_file, drive_file)] - same extension
    2. cross_format_matches: [(sd_file, drive_file, cross_format_type)] - different extensions
       where cross_format_type is like "RAF->JPG" or "JPG->RAF"
    """
    exact_matches = []
    cross_format_matches = []
    
    print(f"Comparing {len(sd_files)} unique SD card files against drive...")
    vprint("\nVERBOSE MODE: Detailed matching process")
    vprint("=" * 70)
    
    # Build extension-based index for faster lookups
    drive_by_ext = defaultdict(list)
    # Also build a stem-only index for cross-format matching
    drive_by_stem = defaultdict(list)
    
    for (stem, ext), paths in drive_files.items():
        for path in paths:
            drive_by_ext[ext].append((stem, path))
            drive_by_stem[stem].append((ext, path))
    
    vprint(f"\nDrive files grouped by extension:")
    for ext, files in drive_by_ext.items():
        vprint(f"  {ext}: {len(files)} files")
    
    exact_count = 0
    cross_format_count = 0
    no_match_count = 0
    
    # For each SD card file, find all drive files with matching substring
    for (sd_stem, sd_ext), sd_paths in sd_files.items():
        vprint(f"\n--- Checking SD card file stem: '{sd_stem}' (ext: {sd_ext}) ---")
        
        # 1. Look for exact extension matches
        candidate_files = drive_by_ext.get(sd_ext, [])
        vprint(f"  Candidates with same extension {sd_ext}: {len(candidate_files)}")
        
        found_exact = []
        for drive_stem, drive_path in candidate_files:
            if sd_stem in drive_stem:  # Substring match
                found_exact.append((drive_stem, drive_path))
                vprint(f"  ✓ EXACT MATCH: '{sd_stem}' found in '{drive_stem}' (both {sd_ext})")
        
        # 2. Look for cross-format matches (different extensions)
        found_cross = []
        for other_ext, files in drive_by_ext.items():
            if other_ext == sd_ext:
                continue  # Skip same extension (already handled above)
            
            # Skip blacklisted extensions
            if sd_ext in BLACKLIST_EXTENSIONS or other_ext in BLACKLIST_EXTENSIONS:
                vprint(f"  ⊘ SKIPPED (blacklisted): {sd_ext} -> {other_ext}")
                continue
            
            for drive_stem, drive_path in files:
                if sd_stem in drive_stem:  # Substring match
                    found_cross.append((drive_stem, drive_path, other_ext))
                    vprint(f"  ✓ CROSS-FORMAT: '{sd_stem}' found in '{drive_stem}' ({sd_ext} -> {other_ext})")
        
        # Add matches for each SD card file with this stem
        for sd_path in sd_paths:
            if found_exact:
                exact_matches.append((sd_path, found_exact[0][1]))
                exact_count += 1
                vprint(f"  → Added exact match: {sd_path.name} -> {found_exact[0][1].name}")
            
            if found_cross:
                for drive_stem, drive_path, other_ext in found_cross:
                    format_type = f"{sd_ext} -> {other_ext}"
                    cross_format_matches.append((sd_path, drive_path, format_type))
                    cross_format_count += 1
                    vprint(f"  → Added cross-format: {sd_path.name} -> {drive_path.name} ({format_type})")
            
            if not found_exact and not found_cross:
                no_match_count += 1
                vprint(f"  ✗ NO MATCH for '{sd_stem}' with extension {sd_ext}")
    
    vprint("\n" + "=" * 70)
    vprint(f"SUMMARY:")
    vprint(f"  Exact matches (same format): {exact_count}")
    vprint(f"  Cross-format matches: {cross_format_count}")
    vprint(f"  No matches: {no_match_count}")
    vprint("=" * 70 + "\n")
    
    return exact_matches, cross_format_matches


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
    
    # Display blacklist info if there are any blacklisted extensions
    if BLACKLIST_EXTENSIONS:
        print(f"\nNote: Excluding {len(BLACKLIST_EXTENSIONS)} blacklisted extensions from cross-format matching:")
        print(f"  {', '.join(sorted(BLACKLIST_EXTENSIONS))}")
        print("  (Edit BLACKLIST_EXTENSIONS in the script to customize)\n")
    
    exact_matches, cross_format_matches = find_matches(sd_files, drive_files)
    
    total_matches = len(exact_matches) + len(cross_format_matches)
    
    if total_matches == 0:
        print("No matching files found on the selected drive.")
        return
    
    print(f"Found {total_matches} total matches:")
    print(f"  - {len(exact_matches)} exact format matches (same extension)")
    print(f"  - {len(cross_format_matches)} cross-format matches (different extensions)")
    print()
    
    # Step 6: Display matches
    print("=" * 80)
    print("MATCHES FOUND")
    print("=" * 80)
    
    # Display exact matches
    if exact_matches:
        print("\n📸 EXACT FORMAT MATCHES (Same Extension)")
        print("-" * 80)
        print("SD Card File -> Drive File")
        print("-" * 80)
        
        display_limit = 20
        for sd_file, drive_file in exact_matches[:display_limit]:
            print(f"{sd_file.name} -> {drive_file.name}")
        
        if len(exact_matches) > display_limit:
            print(f"... and {len(exact_matches) - display_limit} more exact matches")
        print()
    
    # Display cross-format matches and let user filter them
    filtered_cross_format_matches = []
    if cross_format_matches:
        print("\n🔄 CROSS-FORMAT MATCHES (Different Extensions)")
        print("-" * 80)
        print("SD Card File -> Drive File (Format Conversion)")
        print("-" * 80)
        
        # Group by format type for better readability
        by_format = defaultdict(list)
        for sd_file, drive_file, format_type in cross_format_matches:
            by_format[format_type].append((sd_file, drive_file))
        
        display_limit = 20
        shown = 0
        for format_type, files in sorted(by_format.items()):
            print(f"\n  [{format_type}] - {len(files)} files")
            for sd_file, drive_file in files:
                if shown < display_limit:
                    print(f"  {sd_file.name} -> {drive_file.name}")
                    shown += 1
        
        if len(cross_format_matches) > display_limit:
            print(f"\n... and {len(cross_format_matches) - display_limit} more cross-format matches")
        print()
        
        # Let user filter which format types they want
        print("\n" + "=" * 80)
        print("FILTER CROSS-FORMAT MATCHES")
        print("=" * 80)
        print("Select which format conversions you want to include:\n")
        
        selected_formats = []
        for format_type in sorted(by_format.keys()):
            count = len(by_format[format_type])
            response = input(f"Include [{format_type}] ({count} files)? (y/n): ").lower().strip()
            if response == 'y':
                selected_formats.append(format_type)
                print(f"  ✓ Will include {format_type}")
            else:
                print(f"  ✗ Will skip {format_type}")
        
        # Build filtered list based on selected formats
        filtered_cross_format_matches = [
            (sd, drive, fmt) for sd, drive, fmt in cross_format_matches 
            if fmt in selected_formats
        ]
        
        print()
        print(f"Selected {len(filtered_cross_format_matches)} cross-format matches from {len(selected_formats)} format types.")
        print()
    
    print("=" * 80)
    print()
    
    # Step 7: Ask what to rename
    print("What would you like to rename?")
    print(f"  1. Only exact format matches ({len(exact_matches)} files)")
    print(f"  2. Only filtered cross-format matches ({len(filtered_cross_format_matches)} files)")
    print(f"  3. Both exact and filtered cross-format matches ({len(exact_matches) + len(filtered_cross_format_matches)} files)")
    print("  4. Cancel (don't rename anything)")
    
    while True:
        choice = input("\nEnter your choice (1-4): ").strip()
        if choice in ['1', '2', '3', '4']:
            break
        print("Invalid choice. Please enter 1, 2, 3, or 4.")
    
    if choice == '4':
        print("Operation canceled.")
        return
    
    # Determine which files to rename based on choice
    files_to_rename = []
    if choice == '1':
        files_to_rename = exact_matches
        print(f"\nWill rename {len(files_to_rename)} exact format matches.")
    elif choice == '2':
        files_to_rename = [(sd, drive) for sd, drive, _ in filtered_cross_format_matches]
        print(f"\nWill rename {len(files_to_rename)} filtered cross-format matches.")
    elif choice == '3':
        files_to_rename = exact_matches + [(sd, drive) for sd, drive, _ in filtered_cross_format_matches]
        print(f"\nWill rename {len(files_to_rename)} total matches (exact + filtered cross-format).")
    
    if not files_to_rename:
        print("No files selected for renaming.")
        return
    
    # Final confirmation
    if not prompt_user(f"\nProceed with renaming {len(files_to_rename)} files with prefix '{rename_prefix}'?"):
        print("Operation canceled.")
        return
    
    print()
    print("Renaming files...")
    renamed_count = rename_files(files_to_rename, rename_prefix)
    
    print()
    print("=" * 60)
    print(f"Operation complete! Renamed {renamed_count} of {len(files_to_rename)} files.")
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
