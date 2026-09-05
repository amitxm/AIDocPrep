import os
import re
import sys
import datetime
import threading
import concurrent.futures
import urllib.request
import json
from markitdown import MarkItDown

SUPPORTED_EXTENSIONS = [".docx", ".pdf", ".pptx", ".xlsx", ".xls", ".msg", ".epub", ".ipynb", ".vtt", ".html", ".htm"]

# Images convert only when OCR is enabled (see backend/ocr.py); without it an
# image yields no text, so converting one would silently write an empty file.
# They stay out of folder scans even with OCR on — a photo folder would mean
# minutes of OCR for mostly empty results. Drop image files directly instead.
IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"]

IMAGE_UNSUPPORTED_MESSAGE = (
    "Images need OCR — enable \"Read text in images and scanned PDFs\" in Settings"
)

# Everything convert_file will accept. Anything else is rejected up front so
# no file ever reaches a converter with side effects — markitdown's audio
# path, for example, sends audio to a cloud speech API when run from source.
CONVERTIBLE_EXTENSIONS = SUPPORTED_EXTENSIONS + [".txt", ".csv", ".json", ".md"]

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
_PLAIN_TEXT_EXTENSIONS = (".html", ".htm", ".vtt", ".txt", ".csv", ".json", ".md", ".ipynb")
# EPUB is a zip of XHTML; the raw representation is the uncompressed markup
_EPUB_CONTENT_SUFFIXES = (".xhtml", ".html", ".htm")
# Zip-of-XML Office formats where the XML *is* the content (documents and
# sheets). PPTX is handled separately: slide DrawingML is mostly geometry, so
# an XML baseline overstates savings by orders of magnitude on image-heavy
# decks.
_OOXML_EXTENSIONS = (".docx", ".xlsx")


# LLM providers process a raw-uploaded PDF as its extracted text PLUS a
# rendered image of every page; a full page image costs ~1,500 tokens
# (Anthropic documents 1,500-3,000 total per page depending on text density).
# Decks uploaded raw get the same page-image treatment, one image per slide.
_PAGE_IMAGE_TOKENS = 1500


def estimate_source_tokens(file_path: str, output_tokens: int = 0):
    """Rough token estimate of the file's raw representation, used to show
    savings vs the converted Markdown. Text formats use raw bytes; Word/Excel
    use the uncompressed XML; PDFs and decks use extracted text
    (~= output_tokens) plus the per-page image cost providers charge for raw
    uploads. Returns None where no honest baseline exists."""
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
        if ext == ".epub":
            import zipfile
            total = 0
            with zipfile.ZipFile(file_path) as z:
                for info in z.infolist():
                    if info.filename.lower().endswith(_EPUB_CONTENT_SUFFIXES):
                        total += info.file_size  # uncompressed markup
            return max(1, total // 4) if total else None
        if ext == ".pptx":
            import re
            import zipfile
            with zipfile.ZipFile(file_path) as z:
                slides = sum(1 for name in z.namelist()
                             if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
            return slides * _PAGE_IMAGE_TOKENS + output_tokens if slides else None
        if ext == ".pdf":
            from pdfminer.pdfpage import PDFPage
            with open(file_path, "rb") as f:
                pages = sum(1 for _ in PDFPage.get_pages(f))
            return pages * _PAGE_IMAGE_TOKENS + output_tokens if pages else None
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

    # Email (domain is dot-separated labels; the final label must not trail a
    # dot, so a sentence-ending period after the address is preserved)
    text = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+', '[REDACTED_EMAIL]', text)

    # SSN (XXX-XX-XXXX or XXX XX XXXX)
    text = re.sub(r'\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b', '[REDACTED_SSN]', text)

    # Credit Card (16 digits with optional spaces or dashes)
    text = re.sub(r'\b(?:\d{4}[-\s]?){3}\d{4}\b', '[REDACTED_CC]', text)

    # Phone (US/Generic: (555) 123-4567, 555-123-4567, etc.). The area code is
    # matched as an explicit "(555)"-or-"555" alternation so a wrapping paren
    # (and a leading +) are consumed rather than left dangling.
    text = re.sub(r'(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)', '[REDACTED_PHONE]', text)

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

# ZIP conversion is opt-in and never delegated to markitdown's archive
# converter: entries are filtered through the same extension allowlist, capped,
# and extracted under generated names (the archive's own paths are never used
# for writing, so zip-slip is structurally impossible). Nested archives skipped.
ZIP_MAX_ENTRIES = 200
ZIP_MAX_TOTAL_BYTES = 200 * 1024 * 1024
ZIP_MAX_ENTRY_BYTES = 50 * 1024 * 1024


def _convert_zip_archive(file_path: str) -> str:
    import zipfile
    import tempfile

    parts = [f"# Archive: {os.path.basename(file_path)}"]
    skipped = []

    with zipfile.ZipFile(file_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_ENTRIES:
            raise ValueError(f"Archive has {len(infos)} entries; limit is {ZIP_MAX_ENTRIES}")
        declared_total = sum(i.file_size for i in infos)
        if declared_total > ZIP_MAX_TOTAL_BYTES:
            raise ValueError(f"Archive expands to {declared_total // (1024 * 1024)} MB; limit is {ZIP_MAX_TOTAL_BYTES // (1024 * 1024)} MB")

        for info in infos:
            name = info.filename
            base = os.path.basename(name)
            ext = os.path.splitext(base)[1].lower()
            if not base or base.startswith("~$") or base.startswith("."):
                continue
            if ext == ".zip":
                skipped.append(f"{name} (nested archive)")
                continue
            if ext not in CONVERTIBLE_EXTENSIONS:
                skipped.append(f"{name} (unsupported type)")
                continue
            if info.file_size > ZIP_MAX_ENTRY_BYTES:
                skipped.append(f"{name} (exceeds size cap)")
                continue

            with tempfile.TemporaryDirectory() as td:
                safe_path = os.path.join(td, f"entry{ext}")
                remaining = ZIP_MAX_ENTRY_BYTES
                with z.open(info) as src, open(safe_path, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        if remaining < 0:
                            # header lied about uncompressed size
                            raise ValueError(f"Archive entry {name} exceeds the size cap while decompressing")
                        dst.write(chunk)
                try:
                    # raw inner conversion; YAML/redaction applied once by the
                    # caller over the assembled document
                    text = convert_to_markdown(safe_path)
                except Exception as e:
                    skipped.append(f"{name} (conversion failed: {e})")
                    continue

            parts.append(f"\n---\n## {name}\n\n{text}")

    if skipped:
        shown = ", ".join(skipped[:20])
        more = f" and {len(skipped) - 20} more" if len(skipped) > 20 else ""
        parts.append(f"\n---\n*Skipped entries:* {shown}{more}")

    return "\n".join(parts)


def convert_to_markdown(file_path: str, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3", custom_prompt: str = None, custom_terms: str = None, allow_zip: bool = False, ocr: bool = False) -> str:
    """
    Converts a single file and returns the Markdown as a string.
    Writes nothing to disk — callers decide where (or whether) to persist.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".zip":
        if not allow_zip:
            raise ValueError("Unsupported file type: .zip (ZIP conversion is off by default; enable it in Settings)")
        text_content = _convert_zip_archive(file_path)
    elif ext in IMAGE_EXTENSIONS:
        if not ocr:
            raise ValueError(IMAGE_UNSUPPORTED_MESSAGE)
        from backend import ocr as ocr_mod
        text_content = ocr_mod.ocr_image_file(file_path)
        if not text_content:
            raise ValueError("No readable text found in this image")
    elif ext not in CONVERTIBLE_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext or 'no extension'}")
    elif ext == ".vtt":
        text_content = parse_vtt(file_path)
    else:
        result = _get_markitdown().convert(file_path)
        text_content = result.text_content

        if ocr:
            from backend import ocr as ocr_mod
            # Scanned PDFs have no text layer to extract; OCR the pages instead
            if ext == ".pdf" and ocr_mod.pdf_text_layer_is_thin(file_path, text_content):
                scanned = ocr_mod.ocr_pdf(file_path)
                if scanned:
                    text_content = (text_content or "").rstrip() + scanned
            # Slides and documents often carry their content inside screenshots
            elif ext in (".pptx", ".docx", ".xlsx"):
                found = ocr_mod.ocr_embedded_images(file_path)
                if found:
                    text_content = (text_content or "").rstrip() + found
        elif ext in (".pdf", ".pptx", ".docx", ".xlsx"):
            # OCR off: tell the reader which files hold text we didn't read, so a
            # deck of screenshots doesn't look complete when it isn't.
            from backend import ocr as ocr_mod
            unread = ocr_mod.count_unread_images(file_path, text_content)
            if unread:
                text_content = (text_content or "").rstrip() + ocr_mod.unread_images_note(unread, ext)

    if inject_yaml:
        text_content = generate_yaml_frontmatter(file_path) + text_content

    if redact_pii:
        text_content = redact_pii_content(text_content, mode=redact_mode, ollama_model=ollama_model, custom_prompt=custom_prompt, custom_terms=custom_terms)

    return text_content


def convert_file(file_path: str, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3", custom_prompt: str = None, custom_terms: str = None, allow_zip: bool = False, ocr: bool = False) -> str:
    """
    Converts a single file to Markdown using markitdown (or custom VTT parser).
    Returns the path to the generated .md file.
    """
    text_content = convert_to_markdown(
        file_path,
        inject_yaml=inject_yaml,
        redact_pii=redact_pii,
        redact_mode=redact_mode,
        ollama_model=ollama_model,
        custom_prompt=custom_prompt,
        custom_terms=custom_terms,
        allow_zip=allow_zip,
        ocr=ocr,
    )

    output_path = f"{os.path.splitext(file_path)[0]}.md"
    if not overwrite:
        output_path = get_unique_filename(output_path)

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


def convert_files(file_paths: list[str], progress_callback=None, cancel_check=None, overwrite: bool = True, inject_yaml: bool = False, redact_pii: bool = False, redact_mode: str = "Regex Only", ollama_model: str = "llama3", custom_prompt: str = None, custom_terms: str = None, allow_zip: bool = False, ocr: bool = False) -> list[str]:
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
        return convert_file(file_path, overwrite, inject_yaml, redact_pii, redact_mode, ollama_model, custom_prompt, custom_terms, allow_zip=allow_zip, ocr=ocr)

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
            event = {"file": file_path, "status": "done", "output": None, "tokens": 0, "source_tokens": None, "unread_images": 0, "error": None, "done": done_count, "total": total}
            try:
                out_path = future.result()
                converted_files.append(out_path)
                event["output"] = out_path
                out_text = ""
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        out_text = f.read()
                    event["tokens"] = estimate_tokens(out_text)
                except OSError:
                    pass
                event["source_tokens"] = estimate_source_tokens(file_path, output_tokens=event["tokens"])
                if not ocr and os.path.splitext(file_path)[1].lower() in (".pdf", ".pptx", ".docx", ".xlsx"):
                    # Lets UIs flag which files in a batch hold text OCR would have read
                    from backend import ocr as ocr_mod
                    event["unread_images"] = ocr_mod.count_unread_images(file_path, out_text)
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
