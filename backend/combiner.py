import os
import glob
import re
import datetime
from backend.converter import get_unique_filename

def generate_yaml_frontmatter(collection_name: str, file_count: int) -> str:
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    yaml = "---\n"
    yaml += f"collection_name: {collection_name}\n"
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


def combine_files(md_files: list[str], output_path: str, base_dir: str = None, overwrite: bool = True, generate_toc: bool = True, inject_yaml: bool = True, collection_name: str = None) -> str:
    """
    Concatenates an explicit list of .md files into a single master file.
    Only the given files are included, so pre-existing notes in the same
    folder are never swept into the output.
    """
    if not md_files:
        return ""

    if base_dir is None:
        try:
            base_dir = os.path.commonpath([os.path.dirname(os.path.abspath(f)) for f in md_files])
        except ValueError:
            base_dir = os.path.dirname(os.path.abspath(md_files[0]))

    if collection_name is None:
        collection_name = os.path.basename(base_dir.rstrip(os.sep)) or "Documents"

    if not overwrite:
        output_path = get_unique_filename(output_path)

    def rel_name(file_path: str) -> str:
        try:
            return os.path.relpath(file_path, base_dir)
        except ValueError:
            return os.path.basename(file_path)

    with open(output_path, "w", encoding="utf-8") as outfile:
        if inject_yaml:
            outfile.write(generate_yaml_frontmatter(collection_name, len(md_files)))

        outfile.write(f"# {collection_name}\n\n")

        if generate_toc:
            outfile.write("## Table of Contents\n\n")
            for file_path in md_files:
                name = rel_name(file_path)
                anchor = slugify(name)
                outfile.write(f"- [{name}](#{anchor})\n")
            outfile.write("\n")

        for file_path in md_files:
            # Use relative path so we know which sub-folder it came from
            outfile.write(f"\n\n---\n## {rel_name(file_path)}\n\n")

            with open(file_path, "r", encoding="utf-8") as infile:
                outfile.write(infile.read())

            outfile.write("\n")

    return output_path


def combine_folder(folder_path: str, output_filename: str = "combined_master.md", overwrite: bool = True, generate_toc: bool = True, inject_yaml: bool = True) -> str:
    """
    Scans a folder for .md files and concatenates them into a single file.
    Kept for compatibility; prefer combine_files with an explicit list.
    """
    if not os.path.isdir(folder_path):
        raise NotADirectoryError(f"Directory not found: {folder_path}")

    output_path = os.path.join(folder_path, output_filename)

    # Ignore any combined files to prevent infinite growth
    search_pattern = os.path.join(folder_path, "**", "*.md")
    md_files = [
        f for f in glob.glob(search_pattern, recursive=True)
        if not f.endswith("-combined.md") and "combined_master" not in os.path.basename(f)
    ]
    md_files.sort()

    if not md_files:
        return ""

    return combine_files(
        md_files,
        output_path,
        base_dir=folder_path,
        overwrite=overwrite,
        generate_toc=generate_toc,
        inject_yaml=inject_yaml,
    )
