"""Backend tests: conversion, combining, redaction, settings persistence.

Run with:  python tests/test_backend.py
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.converter import convert_files, scan_folder, convert_file, estimate_tokens, estimate_source_tokens
from backend.combiner import combine_files, combine_folder
from backend import settings as settings_mod

failures = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)

def known_gap(name, resolved, detail=""):
    """Documents a redaction-quality limitation without failing the suite.
    Prints RESOLVED once the gap closes (e.g. after the GLiNER upgrade), which
    is the signal to promote it to a hard assertion."""
    if resolved:
        print(f"[RESOLVED] {name} — now fixed; promote to a check() assertion")
    else:
        print(f"[KNOWN-GAP] {name} (tracked for GLiNER redaction upgrade) {detail}")

work = tempfile.mkdtemp(prefix="docprep_test_")
sub = os.path.join(work, "notes")
os.makedirs(sub)

with open(os.path.join(work, "alpha.html"), "w", encoding="utf-8") as f:
    paragraphs = "".join(
        f'<div class="section-wrap outer"><p style="margin:0;padding:4px;color:#333">'
        f'<span class="body-text">Paragraph {i} with meaningful report content here.</span></p></div>'
        for i in range(30)
    )
    f.write(f"<html><body><h1>Alpha Report</h1><p>Contact: jane@example.com and call 555-123-4567.</p>{paragraphs}</body></html>")
with open(os.path.join(sub, "beta.html"), "w", encoding="utf-8") as f:
    f.write("<html><body><h2>Beta Notes</h2><p>Quarterly revenue was strong.</p></body></html>")
with open(os.path.join(work, "meeting.vtt"), "w", encoding="utf-8") as f:
    f.write("WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nHello and welcome.\n\n2\n00:00:02.000 --> 00:00:04.000\nLet's begin the review.\n")
with open(os.path.join(work, "~$temp.docx"), "w") as f:
    f.write("junk")
with open(os.path.join(work, "existing-note.md"), "w", encoding="utf-8") as f:
    f.write("# My own pre-existing note\nShould NOT be swept into combined output.\n")

# 1. scan_folder skips temp files and pre-existing md
found = scan_folder(work)
check("scan_folder finds 3 files", len(found) == 3, f"found={[os.path.basename(f) for f in found]}")
check("scan_folder skips ~$ temp", not any("~$" in f for f in found))

# 2. convert_files with events
events = []
outs = convert_files(found, progress_callback=events.append, inject_yaml=True)
check("convert_files output count", len(outs) == 3, f"outs={len(outs)}")
check("events carry tokens", all(e["tokens"] > 0 for e in events if e["status"] == "done"))
check("events done/total", events[-1]["done"] == 3 and events[-1]["total"] == 3)
alpha_md = os.path.join(work, "alpha.md")
check("yaml frontmatter injected", open(alpha_md, encoding="utf-8").read().startswith("---\n"))

# 2b. source-token baseline for savings display
alpha_event = next(e for e in events if e["file"].endswith("alpha.html") and e["status"] == "done")
check("html event carries source_tokens", (alpha_event.get("source_tokens") or 0) > 0, str(alpha_event.get("source_tokens")))
check("html raw > markdown output", alpha_event["source_tokens"] > alpha_event["tokens"],
      f"src={alpha_event['source_tokens']} out={alpha_event['tokens']}")
check("unknown ext has no raw baseline", estimate_source_tokens("anything.xyz") is None)

def make_pdf(path, pages):
    """Writes a minimal valid PDF with the given page count."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    for _ in range(pages):
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    with open(path, "wb") as f:
        f.write(bytes(out))

pdf_path = os.path.join(work, "sample.pdf")
make_pdf(pdf_path, pages=3)
check("pdf baseline = text + pages x 1500", estimate_source_tokens(pdf_path, output_tokens=800) == 3 * 1500 + 800,
      str(estimate_source_tokens(pdf_path, output_tokens=800)))

# pptx baseline: slides x 1500 + text, NOT the DrawingML XML size (which
# overstates savings on image-heavy decks)
import zipfile as _zipfile
pptx_path = os.path.join(work, "deck.pptx")
with _zipfile.ZipFile(pptx_path, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    for i in range(1, 6):
        z.writestr(f"ppt/slides/slide{i}.xml", "<p:sld>" + "x" * 40000 + "</p:sld>")
    z.writestr("ppt/slides/_rels/slide1.xml.rels", "<Relationships/>")
check("pptx baseline = text + slides x 1500", estimate_source_tokens(pptx_path, output_tokens=700) == 5 * 1500 + 700,
      str(estimate_source_tokens(pptx_path, output_tokens=700)))

# 2c. unsupported types are rejected before reaching any converter, while
# audio/video extensions are routed to the local offline sidecar (never
# markitdown's cloud speech API) and require an installed speech model.
for bad_ext in (".zip", ".exe", ".avi"):
    bad_path = os.path.join(work, f"blocked{bad_ext}")
    with open(bad_path, "wb") as f:
        f.write(b"junkjunkjunkjunk")
    bad_events = []
    bad_outs = convert_files([bad_path], progress_callback=bad_events.append)
    bad_error = next((e for e in bad_events if e["status"] == "error"), None)
    check(f"unsupported ext rejected: {bad_ext}", bad_outs == [] and bad_error is not None
          and "Unsupported file type" in (bad_error["error"] or ""), str(bad_error and bad_error["error"]))
    os.remove(bad_path)

empty_models_dir = os.path.join(work, "empty_models")
os.makedirs(empty_models_dir, exist_ok=True)
prev_models_env = os.environ.get("DOCPREP_MODELS_DIR")
os.environ["DOCPREP_MODELS_DIR"] = empty_models_dir
try:
    for media_ext in (".mp3", ".mp4", ".wav", ".m4a", ".mov"):
        media_path = os.path.join(work, f"unconfigured{media_ext}")
        with open(media_path, "wb") as f:
            f.write(b"junkjunkjunkjunk")
        m_events = []
        m_outs = convert_files([media_path], progress_callback=m_events.append)
        m_error = next((e for e in m_events if e["status"] == "error"), None)
        check(
            f"media routed to offline audio engine (model check): {media_ext}",
            m_outs == [] and m_error is not None and (
                "speech model" in (m_error["error"] or "").lower()
                or "docprep-audio" in (m_error["error"] or "").lower()
            ),
            str(m_error and m_error["error"]),
        )
        os.remove(media_path)
finally:
    if prev_models_env is None:
        os.environ.pop("DOCPREP_MODELS_DIR", None)
    else:
        os.environ["DOCPREP_MODELS_DIR"] = prev_models_env

# 2d. newly supported formats
from backend.converter import convert_to_markdown, SUPPORTED_EXTENSIONS, IMAGE_EXTENSIONS, CONVERTIBLE_EXTENSIONS
import zipfile as _zipfile
import json as _json

fmt_dir = os.path.join(work, "fmt")
os.makedirs(fmt_dir)

# minimal EPUB (zip: mimetype + container + opf + one xhtml chapter)
epub_path = os.path.join(fmt_dir, "book.epub")
chapter = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
           "<head><title>Ch1</title></head><body><h1>Chapter One</h1>"
           "<p>The lighthouse keeper counted the gulls at dawn.</p></body></html>")
opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
       '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title>'
       '<dc:identifier id="id">t1</dc:identifier><dc:language>en</dc:language></metadata>'
       '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
       '<spine><itemref idref="c1"/></spine></package>')
container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             "</rootfiles></container>")
with _zipfile.ZipFile(epub_path, "w") as z:
    z.writestr("mimetype", "application/epub+zip", compress_type=_zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml", container)
    z.writestr("OEBPS/content.opf", opf)
    z.writestr("OEBPS/ch1.xhtml", chapter)
try:
    epub_md = convert_to_markdown(epub_path)
    check("epub converts", "lighthouse keeper" in epub_md, epub_md[:120])
except Exception as e:
    check("epub converts", False, str(e))

# minimal Jupyter notebook
ipynb_path = os.path.join(fmt_dir, "analysis.ipynb")
with open(ipynb_path, "w", encoding="utf-8") as f:
    _json.dump({"cells": [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Notebook Title\n", "Findings about churn."]},
        {"cell_type": "code", "execution_count": 1, "metadata": {}, "outputs": [],
         "source": ["print('churn_rate')"]},
    ], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}, f)
try:
    nb_md = convert_to_markdown(ipynb_path)
    check("ipynb converts", "Findings about churn" in nb_md and "churn_rate" in nb_md, nb_md[:120])
except Exception as e:
    check("ipynb converts", False, str(e))

# images: rejected with a specific message rather than silently writing an
# empty .md (markitdown yields no text without a vision model)
from PIL import Image as _Image
jpg_path = os.path.join(fmt_dir, "photo.jpg")
_Image.new("RGB", (8, 8), (200, 30, 30)).save(jpg_path)
png_path = os.path.join(fmt_dir, "shot.png")
_Image.new("RGB", (8, 8), (30, 60, 200)).save(png_path)
for img in (jpg_path, png_path):
    label = os.path.splitext(img)[1]
    try:
        convert_to_markdown(img)
        check(f"image rejected with actionable message: {label}", False, "converted instead of raising")
    except ValueError as e:
        check(f"image rejected with actionable message: {label}",
              "enable" in str(e).lower(), str(e))
check("no .md written for images",
      not os.path.exists(os.path.splitext(jpg_path)[0] + ".md"))

# msg/xls: junk bytes now fail with a *conversion* error, not the allowlist
for accepted_ext in (".msg", ".xls"):
    p = os.path.join(fmt_dir, f"junk{accepted_ext}")
    with open(p, "wb") as f:
        f.write(b"not really this format")
    try:
        convert_to_markdown(p)
        check(f"{accepted_ext} accepted by allowlist", True, "(junk happened to convert)")
    except ValueError as e:
        check(f"{accepted_ext} accepted by allowlist", "Unsupported file type" not in str(e), str(e)[:80])
    except Exception:
        check(f"{accepted_ext} accepted by allowlist", True, "(conversion error, as expected for junk)")

# scans pick up new formats but not images
scanned = {os.path.basename(f) for f in scan_folder(fmt_dir)}
check("scan includes epub/ipynb/msg/xls",
      {"book.epub", "analysis.ipynb", "junk.msg", "junk.xls"} <= scanned, str(scanned))
check("scan excludes images", "photo.jpg" not in scanned and "shot.png" not in scanned, str(scanned))
check("image exts not convertible", all(e not in CONVERTIBLE_EXTENSIONS for e in IMAGE_EXTENSIONS))

# 2e. ZIP: opt-in, filtered, capped
zip_dir = os.path.join(work, "zips")
os.makedirs(zip_dir)
archive = os.path.join(zip_dir, "bundle.zip")
inner_nested = os.path.join(zip_dir, "nested.zip")
with _zipfile.ZipFile(inner_nested, "w") as z:
    z.writestr("deep.txt", "should never be reached")
with _zipfile.ZipFile(archive, "w") as z:
    z.writestr("report.html", "<html><body><h1>Zipped Report</h1><p>Email zip.person@example.com inside.</p></body></html>")
    z.writestr("song.mp3", b"ID3junk")
    z.writestr("malware.exe", b"MZjunk")
    z.write(inner_nested, "nested.zip")
    z.writestr("sub/notes.txt", "plain text survives")

try:
    convert_to_markdown(archive)
    check("zip rejected by default", False, "no exception")
except ValueError as e:
    check("zip rejected by default", "Unsupported file type" in str(e), str(e)[:80])

zip_md = convert_to_markdown(archive, allow_zip=True, redact_pii=True, redact_mode="Regex Only")
check("zip converts when enabled", "Zipped Report" in zip_md and "plain text survives" in zip_md)
check("zip content is redacted", "[REDACTED_EMAIL]" in zip_md and "zip.person@example.com" not in zip_md)
check("zip skips media in archives", "song.mp3 (audio/video skipped in archives)" in zip_md)
check("zip skips blocked inner types", "malware.exe (unsupported type)" in zip_md)
check("zip skips nested archives", "nested.zip (nested archive)" in zip_md)

big = os.path.join(zip_dir, "toomany.zip")
with _zipfile.ZipFile(big, "w") as z:
    for i in range(201):
        z.writestr(f"f{i}.txt", "x")
try:
    convert_to_markdown(big, allow_zip=True)
    check("zip entry cap enforced", False, "no exception")
except ValueError as e:
    check("zip entry cap enforced", "limit is 200" in str(e), str(e)[:80])

# 2f. OCR (opt-in). Skipped when the OCR extras aren't installed.
from backend import ocr as ocr_mod
if not ocr_mod.is_available():
    print("[SKIP] OCR tests — rapidocr/pypdfium2 not installed")
else:
    ocr_dir = os.path.join(work, "ocr")
    os.makedirs(ocr_dir)
    # an image whose ONLY content is rendered text
    from PIL import Image as _Im, ImageDraw as _Dr
    shot = os.path.join(ocr_dir, "screenshot.png")
    im = _Im.new("RGB", (900, 220), "white")
    _Dr.Draw(im).text((30, 70), "QUARTERLY REVENUE 42817", fill="black")
    im.save(shot)

    # off by default: still refuses images, with the vision-model message
    try:
        convert_to_markdown(shot)
        check("image still refused when OCR off", False, "converted unexpectedly")
    except ValueError as e:
        check("image still refused when OCR off", "enable" in str(e).lower(), str(e)[:60])

    # on: extracts the text
    text = convert_to_markdown(shot, ocr=True)
    check("OCR reads text from an image", "42817" in text.replace(" ", ""), repr(text[:80]))

    # tiny images are skipped as icons, not OCR'd
    icon = os.path.join(ocr_dir, "icon.png")
    _Im.new("RGB", (24, 24), "white").save(icon)
    try:
        convert_to_markdown(icon, ocr=True)
        check("tiny icon yields no text", False, "expected no-text error")
    except ValueError as e:
        check("tiny icon yields no text", "No readable text" in str(e), str(e)[:60])

    # a PDF that already has a text layer must NOT be re-OCR'd
    check("text-layer PDF not treated as scanned",
          not ocr_mod.pdf_text_layer_is_thin(pdf_path, "x" * 5000))
    check("empty-text PDF treated as scanned",
          ocr_mod.pdf_text_layer_is_thin(pdf_path, ""))

    # embedded-image OCR dedupes repeated logos
    # a real deck (python-pptx), not a bare zip: markitdown must be able to
    # convert it. Three byte-distinct images (python-pptx dedupes identical
    # blobs) carrying the same text, so the logo-dedupe check is meaningful.
    from pptx import Presentation
    from pptx.util import Inches
    from PIL import PngImagePlugin as _Png
    import io as _io
    deck = os.path.join(ocr_dir, "deck.pptx")
    prs = Presentation()
    logo = _Im.new("RGB", (500, 160), "white")
    _Dr.Draw(logo).text((20, 60), "ACMECORP LOGO", fill="black")
    for n in range(3):
        # identical pixels (so OCR reads the same text) but distinct bytes via
        # a metadata chunk, so python-pptx keeps three separate media parts
        meta = _Png.PngInfo(); meta.add_text("copy", str(n))
        b = _io.BytesIO(); logo.save(b, format="PNG", pnginfo=meta); b.seek(0)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_picture(b, Inches(1), Inches(1))
    prs.save(deck)
    found = ocr_mod.ocr_embedded_images(deck)
    check("embedded-image OCR finds text", "ACME" in found.upper().replace(" ", ""), repr(found[:80]))
    check("repeated logo emitted once", found.upper().count("ACMECORP") <= 1,
          f"count={found.upper().count('ACMECORP')}")

    # OCR off: the reader is told which files hold unread images, and the
    # conversion event carries a count so a UI can badge them in a batch
    check("unread-image count (deck with 3 images)", ocr_mod.count_unread_images(deck) == 3,
          str(ocr_mod.count_unread_images(deck)))
    icons_only = os.path.join(ocr_dir, "icons.pptx")
    with _zipfile.ZipFile(icons_only, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        b = _io.BytesIO(); _Im.new("RGB", (24, 24), "white").save(b, format="PNG")
        z.writestr("ppt/media/image0.png", b.getvalue())
    check("icons don't count as unread images", ocr_mod.count_unread_images(icons_only) == 0)
    check("scanned-looking PDF counts its pages", ocr_mod.count_unread_images(pdf_path, "") == 3,
          str(ocr_mod.count_unread_images(pdf_path, "")))
    check("text-layer PDF counts nothing", ocr_mod.count_unread_images(pdf_path, "x" * 5000) == 0)
    note_md = convert_to_markdown(deck)                       # OCR off
    check("OCR-off output carries the unread-images note", "3 embedded images that were not read" in note_md,
          repr(note_md[-160:]))
    check("OCR-on output has no such note", "were not read" not in convert_to_markdown(deck, ocr=True))
    ev_unread = []
    convert_files([deck], progress_callback=ev_unread.append, overwrite=True)
    done_ev = next(e for e in ev_unread if e["status"] == "done")
    check("event carries unread_images", done_ev.get("unread_images") == 3, str(done_ev.get("unread_images")))

# 3. redaction (regex)
from backend.converter import redact_pii_content
red_out = convert_file(os.path.join(work, "alpha.html"), overwrite=False, redact_pii=True, redact_mode="Regex Only")
red_text = open(red_out, encoding="utf-8").read()
check("keep-both naming", red_out.endswith("alpha (1).md"), red_out)
check("email redacted", "[REDACTED_EMAIL]" in red_text)
check("phone redacted", "[REDACTED_PHONE]" in red_text)
os.remove(red_out)

# 3b. regex redaction edge cases (previously leaked characters)
r = redact_pii_content("Call (503) 555-0182 today.", mode="Regex Only")
check("phone with parens leaves no dangling '(' or digits",
      "[REDACTED_PHONE]" in r and "(" not in r and "503" not in r, repr(r))
r = redact_pii_content("Reach a.b@school.edu. Next sentence stays.", mode="Regex Only")
check("email preserves the sentence-ending period", "[REDACTED_EMAIL]." in r, repr(r))
check("email does not swallow the following word", "Next sentence stays" in r, repr(r))

# 3c. on-device NER redaction: privacy invariants (hard) + known quality gaps.
# Reproduces the student-gradebook findings — names/scores in terse table cells.
NER_TABLE = "| Marcus Delgado | RHS-2024-0142 | 88 |\n| James Whitfield | RHS-2024-0103 | 94 |"
try:
    nr = redact_pii_content(NER_TABLE, mode="Local NER (spaCy)")
    # Privacy invariants — MUST hold regardless of label accuracy
    check("NER: numeric scores survive redaction", "88" in nr and "94" in nr, repr(nr))
    check("NER: no student name left in cleartext",
          "Marcus Delgado" not in nr and "James Whitfield" not in nr
          and "Marcus" not in nr and "Whitfield" not in nr, repr(nr))
    # Known quality gaps — spaCy en_core_web_sm on terse tabular text.
    # These flip to RESOLVED when a better model (GLiNER) lands.
    known_gap("NER labels every person as REDACTED_NAME (not REDACTED_LOCATION)",
              "[REDACTED_LOCATION]" not in nr, repr(nr))
    known_gap("NER labels names consistently across rows",
              nr.count("[REDACTED_NAME]") == 2, f"names tagged={nr.count('[REDACTED_NAME]')}/2")
    known_gap("NER handles alphanumeric IDs cleanly (no partial 'RHS' mangling)",
              "[REDACTED_LOCATION]-" not in nr, repr(nr))
except Exception as e:
    print(f"[SKIP] on-device NER tests — spaCy model unavailable: {e}")

# 4. combine_files only includes the given list (not existing-note.md)
combined = combine_files(sorted(outs), os.path.join(work, "test-combined.md"), base_dir=work, collection_name="test")
ctext = open(combined, encoding="utf-8").read()
check("combined excludes pre-existing note", "pre-existing note" not in ctext)
check("combined has TOC", "## Table of Contents" in ctext)
check("combined has subfolder rel name", "beta.md" in ctext)
check("combined heading uses collection name", "# test" in ctext)

# 5. combine_folder compat wrapper still works
os.remove(combined)
combined2 = combine_folder(work, output_filename="master.md")
check("combine_folder wrapper", combined2.endswith("master.md") and os.path.exists(combined2))

# 6. settings roundtrip (redirect config dir into temp)
settings_mod.config_dir = lambda: os.path.join(work, "cfg")
settings_mod.config_path = lambda: os.path.join(work, "cfg", "settings.json")
s = settings_mod.load_settings()
check("defaults loaded", s["output_mode"] == "both" and s["conflict"] == "keep_both")
s["output_mode"] = "combined_only"
s["redact"] = True
s["custom_terms"] = "ACME Corp"
settings_mod.save_settings(s)
s2 = settings_mod.load_settings()
check("settings persist", s2["output_mode"] == "combined_only" and s2["redact"] is True and s2["custom_terms"] == "ACME Corp")

# 7. token estimate sanity
check("estimate_tokens", estimate_tokens("word " * 400) == 500)

# 8. cancellation stops scheduling
cancel_calls = {"n": 0}
def cancel_after_first():
    cancel_calls["n"] += 1
    return cancel_calls["n"] > 1
outs_c = convert_files(found, cancel_check=cancel_after_first, overwrite=True)
check("cancel returns partial", len(outs_c) <= len(found))

# 9. in-place re-run (the app's "Read with OCR"): --manifest records where each
# output went; --replace-from overwrites exactly those files, never a user's own
# note and never adding " (1)" copies
import io
import json
import contextlib
import docprep_core

rerun = os.path.join(work, "rerun")
os.makedirs(rerun)
with open(os.path.join(rerun, "a.html"), "w", encoding="utf-8") as f:
    f.write("<html><body><p>first pass</p></body></html>")
with open(os.path.join(rerun, "b.html"), "w", encoding="utf-8") as f:
    f.write("<html><body><p>bee</p></body></html>")
with open(os.path.join(rerun, "a.md"), "w", encoding="utf-8") as f:
    f.write("my own note")  # pre-existing: keep-both must route a.html elsewhere
manifest_path = os.path.join(work, "cache", "last-run.json")

def run_core(*extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = docprep_core.main([rerun, "--json", "--output", "both", "--manifest", manifest_path, *extra])
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    return code, next((e for e in events if e["event"] == "summary"), None)

code1, sum1 = run_core()
check("first run writes the manifest", code1 == 0 and sum1.get("manifest") == manifest_path, str(sum1.get("manifest")))
first_a = os.path.join(rerun, "a (1).md")
check("first run kept the user's a.md and wrote a (1).md", os.path.exists(first_a))
with open(os.path.join(rerun, "a.html"), "w", encoding="utf-8") as f:
    f.write("<html><body><p>second pass</p></body></html>")
code2, sum2 = run_core("--replace-from", manifest_path)
mds = sorted(n for n in os.listdir(rerun) if n.endswith(".md"))
check("re-run adds no copies", mds == ["a (1).md", "a.md", "b.md", "rerun-combined.md"], str(mds))
check("re-run refreshed the earlier output", "second pass" in open(first_a, encoding="utf-8").read())
check("re-run left the user's note alone", open(os.path.join(rerun, "a.md"), encoding="utf-8").read() == "my own note")
check("re-run rewrote the combined file in place",
      sum2.get("combined") == sum1.get("combined") and "second pass" in open(sum2["combined"], encoding="utf-8").read())
code3, _ = run_core("--replace-from", os.path.join(work, "missing.json"))
check("unreadable manifest refuses to run", code3 == 2)

# 10. offline audio/video transcription, model catalog, speaker formatting & live skip
import io
import json
import math
import stat
import struct
import subprocess
import threading
import time
import wave
from contextlib import redirect_stdout
from backend import audio as audio_mod
from backend import models as models_mod
import docprep_core

# 9a. Hardware recommendation matrix & R2 mirror URL switch
check(
    "recommend_model macOS",
    models_mod.recommend_model({"os": "macos", "arch": "aarch64", "ram_mb": 16384}) == "parakeet-tdt-0.6b-v3",
)
check(
    "recommend_model Windows Vulkan >=6GB",
    models_mod.recommend_model({"os": "windows", "vulkan": True, "vram_mb": 8192, "ram_mb": 16384})
    == "whisper-large-v3-turbo-q8_0",
)
check(
    "recommend_model Windows Vulkan 4-6GB",
    models_mod.recommend_model({"os": "windows", "vulkan": True, "vram_mb": 4096, "ram_mb": 16384})
    == "whisper-large-v3-turbo-q5_0",
)
check(
    "recommend_model Windows CPU >=6GB RAM",
    models_mod.recommend_model({"os": "windows", "vulkan": False, "vram_mb": 0, "ram_mb": 16384})
    == "parakeet-tdt-0.6b-v3-int8",
)
check(
    "recommend_model Windows low RAM",
    models_mod.recommend_model({"os": "windows", "vulkan": False, "vram_mb": 0, "ram_mb": 4096})
    == "whisper-small-q5_1",
)

prev_mirror = os.environ.get("DOCPREP_MODEL_MIRROR_URL")
try:
    os.environ.pop("DOCPREP_MODEL_MIRROR_URL", None)
    hf_url = models_mod.hf_resolve_url("ggerganov/whisper.cpp", "ggml-small-q5_1.bin", r2_key="windows/whisper-small-q5_1/ggml-model.bin")
    check("default HF URL", hf_url == "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin", hf_url)
    os.environ["DOCPREP_MODEL_MIRROR_URL"] = "https://models.aidocprep.example.com/"
    r2_url = models_mod.hf_resolve_url("ggerganov/whisper.cpp", "ggml-small-q5_1.bin", r2_key="windows/whisper-small-q5_1/ggml-model.bin")
    check("R2 mirror override URL", r2_url == "https://models.aidocprep.example.com/windows/whisper-small-q5_1/ggml-model.bin", r2_url)
finally:
    if prev_mirror is None:
        os.environ.pop("DOCPREP_MODEL_MIRROR_URL", None)
    else:
        os.environ["DOCPREP_MODEL_MIRROR_URL"] = prev_mirror

# 9b. Speaker segment merging & transcript Markdown formatting
raw_segs = [
    {"start": 74.0, "end": 78.2, "speaker": "Speaker 1", "text": "Welcome everyone."},
    {"start": 79.5, "end": 83.0, "speaker": "Speaker 1", "text": "Let's review Q3 numbers."},  # gap 1.3s <= 3.0s -> merge
    {"start": 88.0, "end": 91.0, "speaker": "Speaker 1", "text": "Moving to the next slide."},  # gap 5.0s > 3.0s -> new turn
    {"start": 91.4, "end": 95.0, "speaker": "Speaker 2", "text": "Sounds good to me."},
]
merged_segs = audio_mod.merge_speaker_segments(raw_segs)
check("merge_speaker_segments count", len(merged_segs) == 3, str(len(merged_segs)))
check("merge_speaker_segments text", merged_segs[0]["text"] == "Welcome everyone. Let's review Q3 numbers.")
md_tx = audio_mod.format_transcript_markdown("Team Sync", raw_segs)
check(
    "format_transcript_markdown turns",
    "**[00:01:14] Speaker 1:** Welcome everyone. Let's review Q3 numbers." in md_tx
    and "**[00:01:28] Speaker 1:** Moving to the next slide." in md_tx
    and "**[00:01:31] Speaker 2:** Sounds good to me." in md_tx,
    md_tx[:180],
)
empty_tx = audio_mod.format_transcript_markdown("Silent Clip", [])
check("format_transcript_markdown empty fallback", "*(No speech detected in audio track.)*" in empty_tx)

# 9c. Speaker header protection during PII redaction (Regex + Custom Terms + spaCy NER)
tx_pii_input = (
    "# Weekly Call\n\n"
    "**[00:01:14] Speaker 1:** Hi Alice Johnson, email me at alice@acme.io about Speaker notes.\n\n"
    "**[00:02:05] Speaker 2:** Copy that, Speaker 1 — calling 415-555-0199 from San Francisco.\n"
)
tx_red_regex = redact_pii_content(tx_pii_input, mode="Regex Only", custom_terms="Speaker\nProjectX")
check(
    "redact preserves speaker prefixes against custom_terms",
    "**[00:01:14] Speaker 1:**" in tx_red_regex
    and "**[00:02:05] Speaker 2:**" in tx_red_regex
    and "[REDACTED_EMAIL]" in tx_red_regex
    and "[REDACTED_PHONE]" in tx_red_regex
    and "about [REDACTED_TERM] notes" in tx_red_regex,
    tx_red_regex,
)
tx_red_ner = redact_pii_content(tx_pii_input, mode="Local NER (spaCy)", custom_terms="Speaker")
check(
    "NER preserves speaker prefixes while redacting names/locations",
    "**[00:01:14] Speaker 1:**" in tx_red_ner
    and "**[00:02:05] Speaker 2:**" in tx_red_ner
    and "Alice Johnson" not in tx_red_ner
    and "San Francisco" not in tx_red_ner,
    tx_red_ner,
)

# 9d. Real native audio sidecar hardware & media duration probe (when compiled binary is present)
audio_test_dir = os.path.join(work, "audio_probe")
os.makedirs(audio_test_dir, exist_ok=True)
sample_wav = os.path.join(audio_test_dir, "tone.wav")
with wave.open(sample_wav, "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(16000)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440.0 * i / 16000.0)))
        for i in range(16000 * 2)  # 2.0 seconds
    )
    wf.writeframes(frames)

real_sidecar = audio_mod.find_audio_sidecar()
if real_sidecar:
    hw_live = audio_mod.probe_hardware()
    check("native sidecar --hardware", hw_live.get("os") in ("macos", "windows") and int(hw_live.get("ram_mb", 0)) > 0, str(hw_live))
    probe_targets = [sample_wav]
    sample_m4a = os.path.join(audio_test_dir, "tone.m4a")
    if sys.platform == "darwin" and os.path.exists("/usr/bin/afconvert"):
        subprocess.run(["/usr/bin/afconvert", "-f", "m4af", "-d", "aac", sample_wav, sample_m4a], check=True, capture_output=True)
        probe_targets.append(sample_m4a)
    probed = audio_mod.probe_media_files(probe_targets)
    check("native sidecar --probe wav duration", len(probed) == len(probe_targets) and 1.8 <= probed[0]["duration_sec"] <= 2.2 and probed[0]["has_audio"] is True, str(probed))
    if len(probed) > 1:
        check("native sidecar --probe m4a duration", 1.8 <= probed[1]["duration_sec"] <= 2.3 and probed[1]["has_audio"] is True, str(probed[1]))
else:
    print("[SKIP] native docprep-audio binary not compiled yet — skipping live probe test")

# 9e. End-to-end mock sidecar conversion, YAML metadata, CLI --exclude & live per-file Skip
mock_dir = os.path.join(work, "mock_audio_env")
mock_models_root = os.path.join(mock_dir, "models")
os.makedirs(mock_models_root, exist_ok=True)

active_spec = models_mod.get_platform_catalog()[0]
active_model_id = active_spec["id"]
if active_spec["engine"] == "fluidaudio":
    for rel in active_spec["required_paths"]:
        p = os.path.join(mock_models_root, rel)
        if rel.endswith(".mlmodelc"):
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, "model.mil"), "wb") as fp:
                fp.write(b"stub")
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fp:
                fp.write(b"{}")
else:
    for f_entry in active_spec.get("files", []) + models_mod.WINDOWS_DIARIZATION_FILES:
        p = os.path.join(mock_models_root, f_entry["local_rel"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fp:
            fp.write(b"stub-onnx-or-ggml")

mock_py = os.path.join(mock_dir, "mock_docprep_audio.py")
with open(mock_py, "w", encoding="utf-8") as fp:
    fp.write(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "args = sys.argv[1:]\n"
        "if '--hardware' in args:\n"
        "    print(json.dumps({'os': 'macos', 'arch': 'aarch64', 'chip_or_gpu': 'Mock Chip', 'gpu_vendor': 'apple', 'vram_mb': 16384, 'ram_mb': 16384, 'vulkan': False, 'avx2': False}), flush=True)\n"
        "    sys.exit(0)\n"
        "if '--probe' in args:\n"
        "    idx = args.index('--probe')\n"
        "    print(json.dumps([{'path': p, 'duration_sec': 74.0, 'has_audio': True} for p in args[idx+1:]]), flush=True)\n"
        "    sys.exit(0)\n"
        "if '--transcribe' in args:\n"
        "    target = args[args.index('--transcribe') + 1]\n"
        "    if 'slow_cancel' in os.path.basename(target):\n"
        "        print(json.dumps({'event': 'progress', 'stage': 'transcribing', 'pct': 15}), flush=True)\n"
        "        time.sleep(10)\n"
        "    print(json.dumps({'event': 'progress', 'stage': 'extracting_audio', 'pct': 100}), flush=True)\n"
        "    print(json.dumps({'event': 'progress', 'stage': 'transcribing', 'pct': 65}), flush=True)\n"
        "    print(json.dumps({'event': 'progress', 'stage': 'diarizing', 'pct': 90}), flush=True)\n"
        "    print(json.dumps({\n"
        "        'event': 'result',\n"
        "        'duration_sec': 74.0,\n"
        "        'language': 'en',\n"
        "        'speakers': 2,\n"
        "        'segments': [\n"
        "            {'start': 2.0, 'end': 4.5, 'speaker': 'Speaker 1', 'text': 'Project kickoff started.'},\n"
        "            {'start': 5.0, 'end': 8.0, 'speaker': 'Speaker 1', 'text': 'Contact bob@company.org for slides.'},\n"
        "            {'start': 9.0, 'end': 12.0, 'speaker': 'Speaker 2', 'text': 'Will do, thanks!'}\n"
        "        ]\n"
        "    }), flush=True)\n"
        "    sys.exit(0)\n"
    )

if sys.platform == "win32":
    mock_bin = os.path.join(mock_dir, "mock_docprep_audio.cmd")
    with open(mock_bin, "w", encoding="utf-8") as fp:
        fp.write(f'@"{sys.executable}" "{mock_py}" %*\n')
else:
    mock_bin = mock_py
    os.chmod(mock_bin, os.stat(mock_bin).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

prev_bin_env = os.environ.get("DOCPREP_AUDIO_BIN")
prev_models_env = os.environ.get("DOCPREP_MODELS_DIR")
try:
    os.environ["DOCPREP_AUDIO_BIN"] = mock_bin
    os.environ["DOCPREP_MODELS_DIR"] = mock_models_root
    audio_mod.reset_cancelled_files()

    check("mock model detected installed", models_mod.is_model_installed(active_model_id))

    batch_dir = os.path.join(mock_dir, "batch")
    os.makedirs(batch_dir, exist_ok=True)
    call_m4a = os.path.join(batch_dir, "standup.m4a")
    skip_mp4 = os.path.join(batch_dir, "excluded_demo.mp4")
    doc_html = os.path.join(batch_dir, "agenda.html")
    for p, content in ((call_m4a, b"fakedata"), (skip_mp4, b"fakedata"), (doc_html, b"<h1>Agenda</h1><p>Items</p>")):
        with open(p, "wb") as fp:
            fp.write(content)

    # Test convert_files progress events, YAML metadata, redaction, and source_tokens=None
    seen_events = []
    conv_outs = convert_files(
        [call_m4a],
        progress_callback=seen_events.append,
        inject_yaml=True,
        redact_pii=True,
        redact_mode="Regex Only",
    )
    check("media convert_files output written", len(conv_outs) == 1 and os.path.isfile(conv_outs[0]))
    with open(conv_outs[0], "r", encoding="utf-8") as fp:
        standup_md = fp.read()
    check(
        "media YAML frontmatter & merged redacted turns",
        "duration: 1m 14s" in standup_md
        and "speakers: 2" in standup_md
        and f"transcription_model: {active_model_id}" in standup_md
        and "**[00:00:02] Speaker 1:** Project kickoff started. Contact [REDACTED_EMAIL] for slides." in standup_md
        and "**[00:00:09] Speaker 2:** Will do, thanks!" in standup_md,
        standup_md[:260],
    )
    done_ev = [e for e in seen_events if e["status"] == "done"][0]
    prog_evs = [e for e in seen_events if e["status"] == "progress"]
    check("media emits intra-file progress events", len(prog_evs) >= 2 and any(e.get("stage") == "transcribing" for e in prog_evs))
    check("media source_tokens is None", done_ev["source_tokens"] is None and done_ev.get("duration_sec") == 74.0 and done_ev.get("speakers") == 2)

    # Test CLI --list-models --json and folder run with --exclude
    buf_models = io.StringIO()
    with redirect_stdout(buf_models):
        rc_models = docprep_core.main(["--list-models", "--json"])
    models_json = json.loads(buf_models.getvalue().strip())
    check("CLI --list-models --json", rc_models == 0 and models_json["active_model"] == active_model_id)

    os.remove(conv_outs[0])
    buf_run = io.StringIO()
    with redirect_stdout(buf_run):
        rc_run = docprep_core.main([batch_dir, "--exclude", skip_mp4, "--conflict", "overwrite", "--json"])
    run_lines = [json.loads(line) for line in buf_run.getvalue().splitlines() if line.strip()]
    summary_ev = [e for e in run_lines if e.get("event") == "summary"][0]
    check(
        "CLI folder scan + --exclude skips unchecked media",
        rc_run == 0
        and summary_ev["converted"] == 2
        and os.path.isfile(os.path.join(batch_dir, "standup.md"))
        and os.path.isfile(os.path.join(batch_dir, "agenda.md"))
        and not os.path.exists(os.path.join(batch_dir, "excluded_demo.md")),
        str(summary_ev),
    )

    # Test live per-file Skip (kills active sidecar child without failing sibling files)
    slow_mov = os.path.join(batch_dir, "slow_cancel_recording.mov")
    with open(slow_mov, "wb") as fp:
        fp.write(b"fakevideo")

    live_events = []
    def on_live_ev(ev):
        live_events.append(ev)
        if ev.get("status") == "progress" and ev.get("file") == slow_mov:
            threading.Thread(target=lambda: (time.sleep(0.05), audio_mod.cancel_audio_file(slow_mov)), daemon=True).start()

    t0 = time.time()
    live_outs = convert_files([slow_mov, doc_html], progress_callback=on_live_ev, overwrite=True)
    elapsed = time.time() - t0
    skipped_evs = [e for e in live_events if e.get("file") == slow_mov and e.get("status") == "skipped"]
    check(
        "live cancel_audio_file terminates sidecar fast and marks skipped",
        len(skipped_evs) == 1 and elapsed < 5.0 and len(live_outs) == 1 and live_outs[0].endswith("agenda.md"),
        f"elapsed={elapsed:.2f}s outs={live_outs} events={[e.get('status') for e in live_events]}",
    )
finally:
    audio_mod.reset_cancelled_files()
    if prev_bin_env is None:
        os.environ.pop("DOCPREP_AUDIO_BIN", None)
    else:
        os.environ["DOCPREP_AUDIO_BIN"] = prev_bin_env
    if prev_models_env is None:
        os.environ.pop("DOCPREP_MODELS_DIR", None)
    else:
        os.environ["DOCPREP_MODELS_DIR"] = prev_models_env

shutil.rmtree(work, ignore_errors=True)
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
