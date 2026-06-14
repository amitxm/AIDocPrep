import os
import re
import datetime
import concurrent.futures
import urllib.request
import json
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

def redact_pii_content(text: str, mode: str = "Regex Only", ollama_model: str = "llama3") -> str:
    """Scans text and replaces common PII, API keys, credentials, and network addresses with redacted labels."""
    # ALWAYS run regex patterns first, as they are fast, precise and clear out obvious high-risk tokens
    # SSH / PEM Private Keys
    text = re.sub(r'-----BEGIN [A-Z ]+ PRIVATE KEY-----\s*[\s\S]+?\s*-----END [A-Z ]+ PRIVATE KEY-----', '[REDACTED_PRIVATE_KEY]', text)

    # Email
    text = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[REDACTED_EMAIL]', text)
    
    # SSN (XXX-XX-XXXX or XXX XX XXXX)
    text = re.sub(r'\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b', '[REDACTED_SSN]', text)
    
    # Credit Card (16 digits with optional spaces or dashes)
    text = re.sub(r'\b(?:\d{4}[-\s]?){3}\d{4}\b', '[REDACTED_CC]', text)
    
    # Phone (US/Generic: (555) 123-4567, 555-123-4567, etc.)
    text = re.sub(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b', '[REDACTED_PHONE]', text)
    
    # API Keys & Tokens
    # AWS Access Key ID
    text = re.sub(r'\b(AKIA|ASCA|ASIA)[0-9A-Z]{16}\b', '[REDACTED_AWS_KEY_ID]', text)
    # OpenAI API Key
    text = re.sub(r'\bsk-[a-zA-Z0-9_-]{20,}\b', '[REDACTED_OPENAI_KEY]', text)
    # Slack Tokens
    text = re.sub(r'\bxox[baprs]-[0-9a-zA-Z-]{10,}\b', '[REDACTED_SLACK_TOKEN]', text)
    
    # Generic secret/token/password assignments (e.g. secret = "value" or "password": "value")
    text = re.sub(
        r'([\'\x22]?)\b(api_key|apikey|secret|token|password|passwd|private_key)\b\1(\s*[:=]\s*)([\'\x22]?)([^\x22\'\s]{8,})\4',
        r'\1\2\1\3\4[REDACTED_CREDENTIAL]\4',
        text,
        flags=re.IGNORECASE
    )

    # Database Connection Strings (redacting password in URIs)
    # e.g., postgresql://user:password@localhost:5432/db
    text = re.sub(
        r'\b(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|sqlite|mssql)://([^:]+):([^@\s]+)@([^\s]+)',
        r'\1://\2:[REDACTED_PASSWORD]@\4',
        text,
        flags=re.IGNORECASE
    )

    # IP Address (IPv4)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[REDACTED_IP]', text)
    
    # IP Address (IPv6)
    text = re.sub(r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b', '[REDACTED_IPV6]', text)

    # MAC Address
    text = re.sub(r'\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b', '[REDACTED_MAC]', text)

    # ------------------ Local NER (spaCy) ------------------
    if mode == "Local NER (spaCy)":
        try:
            import spacy
            try:
                nlp = spacy.load("en_core_web_sm")
            except OSError:
                # If model is not found, attempt to download it dynamically
                import spacy.cli
                spacy.cli.download("en_core_web_sm")
                nlp = spacy.load("en_core_web_sm")
            
            # Process in chunks of 100,000 chars to stay safe on memory
            chunk_size = 100000
            chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
            redacted_chunks = []
            for chunk in chunks:
                doc = nlp(chunk)
                ents = sorted(doc.ents, key=lambda e: e.start_char, reverse=True)
                chunk_chars = list(chunk)
                for ent in ents:
                    if ent.label_ == "PERSON":
                        placeholder = "[REDACTED_NAME]"
                    elif ent.label_ == "ORG":
                        placeholder = "[REDACTED_ORG]"
                    elif ent.label_ == "GPE":
                        placeholder = "[REDACTED_LOCATION]"
                    else:
                        continue
                    chunk_chars[ent.start_char:ent.end_char] = list(placeholder)
                redacted_chunks.append("".join(chunk_chars))
            text = "".join(redacted_chunks)
        except Exception as e:
            print(f"Error running local spaCy NER: {e}")

    # ------------------ Local LLM (Ollama) -----------------
    elif mode == "Local LLM (Ollama)":
        try:
            # Check if Ollama is running and process text in chunks to stay within model context window limits
            # Chunking by paragraphs or ~4000 characters
            chunk_size = 4000
            lines = text.split("\n")
            chunks = []
            current_chunk = []
            current_len = 0
            for line in lines:
                if current_len + len(line) + 1 > chunk_size:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_len = len(line)
                else:
                    current_chunk.append(line)
                    current_len += len(line) + 1
            if current_chunk:
                chunks.append("\n".join(current_chunk))

            redacted_chunks = []
            for chunk in chunks:
                if not chunk.strip():
                    redacted_chunks.append(chunk)
                    continue
                
                # Send HTTP request to local Ollama API
                url = "http://localhost:11434/api/generate"
                prompt = (
                    "You are an offline PII redaction assistant. Your task is to redact all personally identifiable information (PII) "
                    "including names of people, organizations, locations, addresses, and any credentials from the user's text.\n"
                    "Replace names with [REDACTED_NAME], organizations with [REDACTED_ORG], locations/addresses with [REDACTED_LOCATION].\n"
                    "Keep all other text, punctuation, and markdown formatting exactly the same. Do not summarize the text. "
                    "Do not add any conversational response, explanations, introduction, or markdown block wrapping. Return ONLY the redacted text.\n\n"
                    f"Text:\n{chunk}"
                )
                
                payload = {
                    "model": ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.0  # Keep it highly deterministic
                    }
                }
                
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                
                with urllib.request.urlopen(req, timeout=30.0) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    redacted_text = res_data.get("response", "").strip()
                    if redacted_text:
                        redacted_chunks.append(redacted_text)
                    else:
                        redacted_chunks.append(chunk)
            text = "\n".join(redacted_chunks)
        except Exception as e:
            print(f"Error running Ollama redaction: {e}")

    return text

def convert_file(file_path: str, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3") -> str:
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
        text_content = redact_pii_content(text_content, mode=redact_mode, ollama_model=ollama_model)
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text_content)
        
    return output_path

def convert_folder(folder_path: str, extensions: list[str] = None, progress_callback=None, cancel_check=None, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3") -> list[str]:
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
            executor.submit(convert_file, file_path, overwrite, inject_yaml, redact_pii, redact_mode, ollama_model): file_path 
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
