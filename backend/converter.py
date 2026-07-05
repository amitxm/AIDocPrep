import os
import re
import sys
import datetime
import threading
import concurrent.futures
import urllib.request
import json
from markitdown import MarkItDown

SUPPORTED_EXTENSIONS = [".docx", ".pdf", ".pptx", ".xlsx", ".vtt", ".html", ".htm"]

DEFAULT_OLLAMA_PROMPT = (
    "You are an offline PII redaction assistant. Your task is to redact all personally identifiable information (PII) "
    "including names of people, organizations, locations, addresses, and any credentials from the user's text.\n"
    "Replace names with [REDACTED_NAME], organizations with [REDACTED_ORG], locations/addresses with [REDACTED_LOCATION].\n"
    "Keep all other text, punctuation, and markdown formatting exactly the same. Do not summarize the text. "
    "Do not add any conversational response, explanations, introduction, or markdown block wrapping. Return ONLY the redacted text."
)

# MarkItDown instances are reused per thread; spaCy and Ollama are shared
# resources that must not be hit from every pool thread at once.
_thread_local = threading.local()
_spacy_lock = threading.Lock()
_ollama_lock = threading.Lock()
_nlp = None
_ollama_unreachable = False

OLLAMA_URL = "http://localhost:11434"


def _ollama_available() -> bool:
    """Fast one-time reachability check so an absent Ollama server costs 2s
    once per run instead of a long timeout per file."""
    global _ollama_unreachable
    if _ollama_unreachable:
        return False
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2.0):
            return True
    except Exception:
        _ollama_unreachable = True
        print("Ollama not reachable at localhost:11434 — skipping LLM redaction (regex patterns still applied).", file=sys.stderr)
        return False


def _get_markitdown() -> MarkItDown:
    md = getattr(_thread_local, "markitdown", None)
    if md is None:
        md = MarkItDown()
        _thread_local.markitdown = md
    return md


def estimate_tokens(text: str) -> int:
    """Rough LLM token estimate (~4 characters per token for English text)."""
    return max(1, len(text) // 4)


# Formats whose raw bytes are text an LLM would actually be fed
_PLAIN_TEXT_EXTENSIONS = (".html", ".htm", ".vtt", ".txt", ".csv", ".json", ".md")
# Zip-of-XML Office formats: the raw representation is the uncompressed XML
_OOXML_EXTENSIONS = (".docx", ".pptx", ".xlsx")


# LLM providers process a raw-uploaded PDF as its extracted text PLUS a
# rendered image of every page; a full page image costs ~1,500 tokens
# (Anthropic documents 1,500-3,000 total per page depending on text density).
_PDF_IMAGE_TOKENS_PER_PAGE = 1500


def estimate_source_tokens(file_path: str, output_tokens: int = 0):
    """Rough token estimate of the file's raw representation, used to show
    savings vs the converted Markdown. Text formats use raw bytes; OOXML uses
    the uncompressed XML; PDF uses extracted text (~= output_tokens) plus the
    per-page image cost providers charge for raw PDF uploads. Returns None
    where no honest baseline exists."""
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext in _PLAIN_TEXT_EXTENSIONS:
            return max(1, os.path.getsize(file_path) // 4)
        if ext in _OOXML_EXTENSIONS:
            import zipfile
            total = 0
            with zipfile.ZipFile(file_path) as z:
                for info in z.infolist():
                    if info.filename.endswith((".xml", ".rels")):
                        total += info.file_size  # uncompressed size
            return max(1, total // 4) if total else None
        if ext == ".pdf":
            from pdfminer.pdfpage import PDFPage
            with open(file_path, "rb") as f:
                pages = sum(1 for _ in PDFPage.get_pages(f))
            return pages * _PDF_IMAGE_TOKENS_PER_PAGE + output_tokens if pages else None
    except Exception:
        return None
    return None


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


def _load_spacy():
    # The model must be bundled with the app (see build scripts); a runtime
    # download can't work in a frozen build and is barred by store sandboxes.
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp

def redact_pii_content(text: str, mode: str = "Regex Only", ollama_model: str = "llama3", custom_prompt: str = None, custom_terms: str = None) -> str:
    """Scans text and replaces common PII, API keys, credentials, and network addresses with redacted labels."""
    # Run custom terms redaction if present
    if custom_terms:
        terms = [t.strip() for t in custom_terms.split("\n") if t.strip()]
        for term in terms:
            escaped_term = re.escape(term)
            text = re.sub(rf'\b{escaped_term}\b', '[REDACTED_TERM]', text, flags=re.IGNORECASE)

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
            with _spacy_lock:
                nlp = _load_spacy()
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
            print(f"Error running local spaCy NER: {e}", file=sys.stderr)

    # ------------------ Local LLM (Ollama) -----------------
    elif mode == "Local LLM (Ollama)" and _ollama_available():
        try:
            # Chunk to stay within model context window limits (~4000 chars per request)
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

            base_prompt = custom_prompt if (custom_prompt and custom_prompt.strip()) else DEFAULT_OLLAMA_PROMPT

            # A local Ollama server processes one generation at a time; the lock
            # keeps pool threads from stacking requests until they time out.
            with _ollama_lock:
                redacted_chunks = []
                for chunk in chunks:
                    if not chunk.strip():
                        redacted_chunks.append(chunk)
                        continue

                    url = f"{OLLAMA_URL}/api/generate"
                    prompt = f"{base_prompt}\n\nText:\n{chunk}"

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

                    with urllib.request.urlopen(req, timeout=120.0) as response:
                        res_data = json.loads(response.read().decode("utf-8"))
                        redacted_text = res_data.get("response", "").strip()
                        if redacted_text:
                            redacted_chunks.append(redacted_text)
                        else:
                            redacted_chunks.append(chunk)
                text = "\n".join(redacted_chunks)
        except Exception as e:
            print(f"Error running Ollama redaction: {e}", file=sys.stderr)

    return text

def convert_file(file_path: str, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3", custom_prompt: str = None, custom_terms: str = None) -> str:
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
        result = _get_markitdown().convert(file_path)
        text_content = result.text_content

    if inject_yaml:
        text_content = generate_yaml_frontmatter(file_path) + text_content

    if redact_pii:
        text_content = redact_pii_content(text_content, mode=redact_mode, ollama_model=ollama_model, custom_prompt=custom_prompt, custom_terms=custom_terms)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text_content)

    return output_path


def scan_folder(folder_path: str, extensions: list[str] = None) -> list[str]:
    """Returns all supported files in a folder tree, skipping Office temp/lock
    files (~$...) and hidden dotfiles."""
    if extensions is None:
        extensions = SUPPORTED_EXTENSIONS

    target_files = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            if file.startswith("~$") or file.startswith("."):
                continue
            ext = os.path.splitext(file)[1].lower()
            if ext in extensions:
                target_files.append(os.path.join(root, file))
    target_files.sort()
    return target_files


def convert_files(file_paths: list[str], progress_callback=None, cancel_check=None, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3", custom_prompt: str = None, custom_terms: str = None) -> list[str]:
    """
    Converts a list of files to Markdown in parallel.

    progress_callback receives one event dict per finished file:
      {"file", "status": "done"|"error", "output", "tokens", "error", "done", "total"}
    cancel_check() returning True stops scheduling new files (in-flight ones finish).
    Returns the list of output paths that were written.
    """
    total = len(file_paths)
    converted_files = []

    # ~75% of CPU cores (minimum 1) so conversion doesn't bog down the machine
    cpu_cores = os.cpu_count() or 4
    optimal_workers = max(1, int(cpu_cores * 0.75))

    def convert_one(file_path):
        if progress_callback:
            # "started" lets UIs show which file is being worked on; heavy
            # files can take a while between completion events
            progress_callback({"file": file_path, "status": "started", "output": None, "tokens": 0, "source_tokens": None, "error": None, "done": None, "total": total})
        return convert_file(file_path, overwrite, inject_yaml, redact_pii, redact_mode, ollama_model, custom_prompt, custom_terms)

    with concurrent.futures.ThreadPoolExecutor(max_workers=optimal_workers) as executor:
        future_to_file = {
            executor.submit(convert_one, file_path): file_path
            for file_path in file_paths
        }

        done_count = 0
        for future in concurrent.futures.as_completed(future_to_file):
            if cancel_check and cancel_check():
                for f in future_to_file:
                    f.cancel()
                break

            file_path = future_to_file[future]
            done_count += 1
            event = {"file": file_path, "status": "done", "output": None, "tokens": 0, "source_tokens": None, "error": None, "done": done_count, "total": total}
            try:
                out_path = future.result()
                converted_files.append(out_path)
                event["output"] = out_path
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        event["tokens"] = estimate_tokens(f.read())
                except OSError:
                    pass
                event["source_tokens"] = estimate_source_tokens(file_path, output_tokens=event["tokens"])
            except Exception as e:
                event["status"] = "error"
                event["error"] = str(e)

            if progress_callback:
                progress_callback(event)

    return converted_files


def convert_folder(folder_path: str, extensions: list[str] = None, progress_callback=None, cancel_check=None, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3", custom_prompt: str = None, custom_terms: str = None) -> list[str]:
    """
    Converts all supported files in a folder to Markdown.
    Kept for compatibility: progress_callback here is the old (current, total) style.
    """
    target_files = scan_folder(folder_path, extensions)

    def event_callback(event):
        if progress_callback and event["status"] != "started":
            progress_callback(event["done"], event["total"])

    return convert_files(
        target_files,
        progress_callback=event_callback if progress_callback else None,
        cancel_check=cancel_check,
        overwrite=overwrite,
        inject_yaml=inject_yaml,
        redact_pii=redact_pii,
        redact_mode=redact_mode,
        ollama_model=ollama_model,
        custom_prompt=custom_prompt,
        custom_terms=custom_terms,
    )
