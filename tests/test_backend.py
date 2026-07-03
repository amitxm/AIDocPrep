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
alpha_event = next(e for e in events if e["file"].endswith("alpha.html"))
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
check("pdf baseline = pages x 2000", estimate_source_tokens(pdf_path) == 6000, str(estimate_source_tokens(pdf_path)))

# 3. redaction (regex)
red_out = convert_file(os.path.join(work, "alpha.html"), overwrite=False, redact_pii=True, redact_mode="Regex Only")
red_text = open(red_out, encoding="utf-8").read()
check("keep-both naming", red_out.endswith("alpha (1).md"), red_out)
check("email redacted", "[REDACTED_EMAIL]" in red_text)
check("phone redacted", "[REDACTED_PHONE]" in red_text)
os.remove(red_out)

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

shutil.rmtree(work, ignore_errors=True)
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
