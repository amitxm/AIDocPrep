import os
import glob
import re
import datetime
from backend.converter import get_unique_filename

def generate_yaml_frontmatter(original_folder: str, file_count: int) -> str:
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    name = os.path.basename(original_folder)
    
    yaml = "---\n"
    yaml += f"collection_name: {name}\n"
    yaml += f"total_files: {file_count}\n"
    yaml += f"date_combined: {date_str}\n"
    yaml += "---\n\n"
    return yaml

def slugify(text: str) -> str:
    """Converts a string into a valid markdown anchor link."""
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text)
    return text

def combine_folder(folder_path: str, output_filename: str = "combined_master.md", overwrite: bool = True, generate_toc: bool = True, inject_yaml: bool = True) -> str:
    """
    Scans a folder for .md files and concatenates them into a single file.
    """
    if not os.path.isdir(folder_path):
        raise NotADirectoryError(f"Directory not found: {folder_path}")
        
    output_path = os.path.join(folder_path, output_filename)
    if not overwrite:
        output_path = get_unique_filename(output_path)
    
    # Get all markdown files in the folder and its subfolders
    # Ignore any combined files to prevent infinite growth
    search_pattern = os.path.join(folder_path, "**", "*.md")
    md_files = [
        f for f in glob.glob(search_pattern, recursive=True) 
        if not f.endswith("-combined.md") and "combined_master" not in os.path.basename(f)
    ]
    
    md_files.sort()
    
    if not md_files:
        return ""
        
    with open(output_path, "w", encoding="utf-8") as outfile:
        if inject_yaml:
            outfile.write(generate_yaml_frontmatter(folder_path, len(md_files)))
            
        outfile.write("# Combined Master Document\n\n")
        
        if generate_toc:
            outfile.write("## Table of Contents\n\n")
            for file_path in md_files:
                rel_name = os.path.relpath(file_path, folder_path)
                anchor = slugify(rel_name)
                outfile.write(f"- [{rel_name}](#{anchor})\n")
            outfile.write("\n")
        
        for file_path in md_files:
            # Use relative path so we know which sub-folder it came from
            rel_name = os.path.relpath(file_path, folder_path)
            outfile.write(f"\n\n---\n## {rel_name}\n\n")
            
            with open(file_path, "r", encoding="utf-8") as infile:
                outfile.write(infile.read())
                
            outfile.write("\n")
            
    return output_path
