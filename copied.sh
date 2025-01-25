#!/bin/zsh

# Function to prompt the user for input
prompt_user() {
    echo "$1"
    read -q "response?Press 'y' to continue or any other key to cancel: "
    echo
    if [[ $response != 'y' ]]; then
        echo "Operation canceled."
        exit 1
    fi
}

# Step 1: Select a drive to search using a file picker
echo "Select the drive to search for copied files."
drive_path=$(osascript -e 'POSIX path of (choose folder with prompt "Select the drive to search for copied files:")')
if [[ ! -d $drive_path ]]; then
    echo "Invalid path: $drive_path"
    exit 1
fi

# Step 2: Specify the SD card directory using a file picker
echo "Select the SD card directory."
sd_card_path=$(osascript -e 'POSIX path of (choose folder with prompt "Select the SD card directory:")')
if [[ ! -d $sd_card_path ]]; then
    echo "Invalid path: $sd_card_path"
    exit 1
fi

# Step 3: Prepend text for renamed files
read "rename_prefix?Enter the prefix to add to already copied files on the SD card (e.g., 'COPIED_'): "

# Find all files on the SD card
sd_files=("$sd_card_path"/**/*(N))
if [[ ${#sd_files[@]} -eq 0 ]]; then
    echo "No files found on the SD card."
    exit 0
fi

# Step 4: Compare and list matches with progress indicator
matches=()
total_files=${#sd_files[@]}
echo "Searching for matches..."
count=0
for sd_file in "$sd_card_path"/**/*(N); do
    ((count++))
    printf "\rProcessing file %d of %d..." "$count" "$total_files"
    sd_filename=$(basename "$sd_file")
    matching_files=("$drive_path"/**/*"$sd_filename"*(N))
    if [[ ${#matching_files[@]} -gt 0 ]]; then
        for match in $matching_files; do
            matches+=("$sd_file|$match")
        done
    fi
done
printf "\n"

if [[ ${#matches[@]} -eq 0 ]]; then
    echo "No matching files found on the selected drive."
    exit 0
fi

# Step 5: Display matches and get confirmation
echo "The following files have been identified as already copied:"
echo "Original (SD Card) -> Copied File (Drive)"
for match in $matches; do
    IFS='|' read sd_file copied_file <<< "$match"
    echo "$sd_file -> $copied_file"
done

prompt_user "Do you want to rename the matched files on the SD card?"

# Step 6: Rename files on the SD card
for match in $matches; do
    IFS='|' read sd_file copied_file <<< "$match"
    sd_dir=$(dirname "$sd_file")
    sd_filename=$(basename "$sd_file")
    new_name="$sd_dir/$rename_prefix$sd_filename"
    mv "$sd_file" "$new_name"
    echo "Renamed: $sd_file -> $new_name"
done

echo "Operation complete. Renamed ${#matches[@]} files."
