"""On-device OCR (RapidOCR / PP-OCRv4 ONNX models).

Opt-in: OCR is ~1000x slower than text extraction, so it only runs when the
user enables it, and only where it can actually help — scanned PDFs with no
text layer, images embedded in slides, and standalone image files.

Everything here imports lazily: when OCR is off, none of these dependencies
are touched and conversion speed is unaffected.
"""
import io
import os
import sys
import time
import zipfile
import threading

# One OCR call at a time. The engine already uses every core for a single
# image, so running several concurrently only oversubscribes the CPU (measured
# ~50% slower than sequential), and it keeps engine initialisation single.
_ocr_lock = threading.Lock()
# DOCPREP_OCR_TRACE=1 prints per-image timings to stderr (support/diagnostics)
_TRACE = os.environ.get("DOCPREP_OCR_TRACE") == "1"

# onnxruntime defaults to one thread per logical core. A spin-waiting pool
# that wide collapses as soon as anything else touches a core — antivirus,
# an indexer, a browser — which is always the case on a user's machine.
# Measured on a 16-thread laptop, same 14-image deck: 16 threads took 13–87s
# depending on background load; 4 threads took 15–25s, consistently. The
# models are small; four threads is plenty.
OCR_THREADS = max(1, min(4, os.cpu_count() or 1))

# Images smaller than this are icons, bullets and logo spacers — never content
MIN_PIXELS = 100 * 100
# Recognition confidence floor
MIN_SCORE = 0.5
# Text shorter than this is noise (stray marks read as "1", "a", ...)
MIN_TEXT_CHARS = 3
# Downscale before OCR: past this, cost grows without recovering more text
MAX_SIDE = 1600
# A PDF page with fewer extracted characters than this is treated as scanned
TEXT_LAYER_CHARS_PER_PAGE = 100

IMAGE_MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR(intra_op_num_threads=OCR_THREADS)
    return _engine


def is_available() -> bool:
    """True if the OCR dependencies are present in this build."""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        import pypdfium2  # noqa: F401
        return True
    except Exception:
        return False


def ocr_image(img) -> str:
    """OCRs a PIL image, returning text in top-to-bottom order. Returns "" for
    images too small to hold content or with nothing legible in them."""
    if img.width * img.height < MIN_PIXELS:
        return ""
    if max(img.size) > MAX_SIDE:
        scale = MAX_SIDE / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)))

    import numpy as np
    t0 = time.perf_counter()
    with _ocr_lock:
        result, _ = _get_engine()(np.array(img.convert("RGB")))
    if _TRACE:
        print(f"[ocr] {img.width}x{img.height} boxes={len(result or [])} "
              f"{time.perf_counter() - t0:.2f}s", file=sys.stderr, flush=True)
    if not result:
        return ""

    lines = []
    for box, text, score in result:
        text = (text or "").strip()
        if score >= MIN_SCORE and text:
            lines.append((min(p[1] for p in box), text))
    lines.sort(key=lambda x: x[0])
    out = "\n".join(t for _, t in lines).strip()
    return out if len(out) >= MIN_TEXT_CHARS else ""


def ocr_image_file(file_path: str) -> str:
    from PIL import Image
    with Image.open(file_path) as img:
        return ocr_image(img)


def pdf_text_layer_is_thin(file_path: str, extracted_text: str) -> bool:
    """True when a PDF looks scanned: far less text than its page count implies."""
    try:
        from pdfminer.pdfpage import PDFPage
        with open(file_path, "rb") as f:
            pages = sum(1 for _ in PDFPage.get_pages(f))
    except Exception:
        return False
    if not pages:
        return False
    return len(extracted_text or "") < pages * TEXT_LAYER_CHARS_PER_PAGE


def ocr_pdf(file_path: str, dpi: int = 200, progress=None) -> str:
    """Rasterizes each page and OCRs it. Used only for PDFs with no text layer."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(file_path)
    parts = []
    try:
        total = len(doc)
        for i in range(total):
            if progress:
                progress(i + 1, total)
            bitmap = doc[i].render(scale=dpi / 72)
            img = bitmap.to_pil()
            try:
                text = ocr_image(img)
            finally:
                img.close()
            if text:
                parts.append(f"\n\n## Page {i + 1}\n\n{text}")
    finally:
        doc.close()
    return "".join(parts)


def ocr_embedded_images(file_path: str, progress=None) -> str:
    """OCRs images embedded in an OOXML file (pptx/docx/xlsx media parts).

    Repeated text is emitted once — decks reuse the same logo on every slide,
    and without dedupe the output is mostly that logo.
    """
    from PIL import Image

    parts = []
    seen = set()
    try:
        with zipfile.ZipFile(file_path) as z:
            media = sorted(
                n for n in z.namelist()
                if "/media/" in n and n.lower().endswith(IMAGE_MEDIA_SUFFIXES)
            )
            for idx, name in enumerate(media, 1):
                if progress:
                    progress(idx, len(media))
                try:
                    with Image.open(io.BytesIO(z.read(name))) as img:
                        text = ocr_image(img)
                except Exception:
                    continue
                if not text:
                    continue
                key = text.strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                parts.append(f"\n\n### {os.path.basename(name)}\n\n{text}")
    except zipfile.BadZipFile:
        return ""

    if not parts:
        return ""
    return "\n\n---\n\n## Text found in images\n" + "".join(parts)
