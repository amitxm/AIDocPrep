import os
import re
import datetime
import concurrent.futures
from markitdown import MarkItDown

def get_unique_filename(base_path: str) -> str:
    """If file exists, appends (1), (2), etc. to the filename to avoid overwriting."""
    if not os.path.exists(base_path):
        return base_path
    
    name, ext = os.path.splitext(base_path)
    counter = 1
    while True:
        new_path = f"{name} ({counter}){ext}"
        if not os.path.exists(new_path):
            return new_path
        counter += 1

def parse_vtt(file_path: str) -> str:
    """Parses a VTT file and extracts only the spoken text, stripping timestamps and indices."""
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    text_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line == "WEBVTT":
            continue
        # Skip cue indices (usually just digits)
        if line.isdigit():
            continue
        # Skip timestamps (e.g. 00:00:00.000 --> 00:00:02.000)
        if "-->" in line:
            continue
            
        text_lines.append(line)
        
    return "\n\n".join(text_lines)

def generate_yaml_frontmatter(original_file: str) -> str:
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    name = os.path.basename(original_file)
    ext = os.path.splitext(original_file)[1].lower()
    
    yaml = "---\n"
    yaml += f"original_filename: {name}\n"
    yaml += f"source_format: {ext}\n"
    yaml += f"date_converted: {date_str}\n"
    yaml += "---\n\n"
    return yaml

def redact_pii_content(text: str) -> str:
    """Scans text and replaces common PII patterns with redacted labels."""
    # Email
    text = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[REDACTED_EMAIL]', text)
    
    # SSN (XXX-XX-XXXX or XXX XX XXXX)
    text = re.sub(r'\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b', '[REDACTED_SSN]', text)
    
    # Credit Card (16 digits with optional spaces or dashes)
    text = re.sub(r'\b(?:\d{4}[-\s]?){3}\d{4}\b', '[REDACTED_CC]', text)
    
    # Phone (US/Generic: (555) 123-4567, 555-123-4567, etc.)
    text = re.sub(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b', '[REDACTED_PHONE]', text)
    
    # IP Address (IPv4)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[REDACTED_IP]', text)
    
    return text

def convert_file(file_path: str, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False) -> str:
    """
    Converts a single file to Markdown using markitdown (or custom VTT parser).
    Returns the path to the generated .md file.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
        
    base_name, ext = os.path.splitext(file_path)
    ext = ext.lower()
    output_path = f"{base_name}.md"
    
    if not overwrite:
        output_path = get_unique_filename(output_path)
        
    if ext == ".vtt":
        text_content = parse_vtt(file_path)
    else:
        md = MarkItDown()
        result = md.convert(file_path)
        text_content = result.text_content
        
    if inject_yaml:
        text_content = generate_yaml_frontmatter(file_path) + text_content
        
    if redact_pii:
        text_content = redact_pii_content(text_content)
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text_content)
        
    return output_path

def convert_folder(folder_path: str, extensions: list[str] = None, progress_callback=None, cancel_check=None, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False) -> list[str]:
    """
    Converts all supported files in a folder to Markdown.
    Accepts an optional progress_callback(current, total) to report status.
    Accepts cancel_check callback that returns True if user cancelled.
    """
    if extensions is None:
        extensions = [".docx", ".pdf", ".pptx", ".xlsx", ".vtt", ".html", ".htm"]
        
    target_files = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in extensions:
                target_files.append(os.path.join(root, file))
                
    total = len(target_files)
    converted_files = []
    
    # Use ThreadPoolExecutor for cross-platform, lightweight parallelization
    # This automatically scales the number of threads based on CPU cores
    # Calculate optimal workers: ~75% of CPU cores (minimum 1) so it doesn't bog down the machine
    cpu_cores = os.cpu_count() or 4
    optimal_workers = max(1, int(cpu_cores * 0.75))
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=optimal_workers) as executor:
        # Submit all file conversion tasks to the thread pool
        future_to_file = {
            executor.submit(convert_file, file_path, overwrite, inject_yaml, redact_pii): file_path 
            for file_path in target_files
        }
        
        # Process results as they complete (out of order, but highly parallel)
        for idx, future in enumerate(concurrent.futures.as_completed(future_to_file)):
            if cancel_check and cancel_check():
                # If the user hit 'Stop', cancel all pending tasks in the queue
                for f in future_to_file:
                    f.cancel()
                break
                
            file_path = future_to_file[future]
            try:
                out_path = future.result()
                converted_files.append(out_path)
            except Exception as e:
                print(f"Error converting {file_path}: {e}")
                
            if progress_callback:
                progress_callback(idx + 1, total)
                    
    return converted_files
