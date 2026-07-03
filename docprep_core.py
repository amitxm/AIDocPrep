"""docprep-core — headless CLI for the AI DocPrep conversion engine.

Converts office documents to Markdown from the command line, and doubles as
the machine-readable engine for GUI shells: with --json it emits one JSON
event per line on stdout (start, file, combined, summary), so any front end
(or script) can stream progress.

Examples:
  python docprep_core.py report.docx slides.pptx
  python docprep_core.py ./research --output combined-only --redact
  python docprep_core.py ./docs --json > events.jsonl
"""
import os
import sys
import json
import time
import signal
import argparse

from backend.converter import convert_files, scan_folder, estimate_tokens
from backend.combiner import combine_files

VERSION = "1.2.0"

ENGINE_NAMES = {
    "regex": "Regex Only",
    "ner": "Local NER (spaCy)",
    "ollama": "Local LLM (Ollama)",
}

OUTPUT_CHOICES = ("individual", "both", "combined-only")

_cancelled = False


def _handle_sigint(signum, frame):
    global _cancelled
    _cancelled = True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docprep-core",
        description="Convert office documents (.docx, .pdf, .pptx, .xlsx, .html, .vtt) to clean Markdown.",
    )
    p.add_argument("paths", nargs="+", help="files and/or folders to convert")
    p.add_argument("--output", choices=OUTPUT_CHOICES, default="individual",
                   help="individual .md files, individual plus a combined file, or the combined file only (default: individual)")
    p.add_argument("--combined-name", default=None, metavar="NAME",
                   help="filename for the combined file (default: <folder>-combined.md)")
    p.add_argument("--no-yaml", action="store_true", help="skip YAML frontmatter")
    p.add_argument("--no-toc", action="store_true", help="skip the table of contents in combined files")
    p.add_argument("--conflict", choices=("keep-both", "overwrite"), default="keep-both",
                   help="what to do when the output file exists (default: keep-both)")
    p.add_argument("--redact", action="store_true", help="redact PII before writing")
    p.add_argument("--engine", choices=tuple(ENGINE_NAMES), default="regex",
                   help="redaction engine (default: regex)")
    p.add_argument("--ollama-model", default="llama3", metavar="MODEL")
    p.add_argument("--prompt-file", default=None, metavar="PATH",
                   help="file containing a custom Ollama redaction prompt")
    p.add_argument("--terms-file", default=None, metavar="PATH",
                   help="file with custom terms to redact, one per line")
    p.add_argument("--json", action="store_true", help="emit JSON-lines events on stdout")
    p.add_argument("--quiet", action="store_true", help="only print the final summary (human mode)")
    p.add_argument("--version", action="version", version=f"docprep-core {VERSION}")
    return p


def collect_files(paths: list[str]) -> tuple[list[str], str]:
    """Expands folders, dedupes, and returns (files, common_base_dir)."""
    files = []
    seen = set()
    dirs = []
    for path in paths:
        path = os.path.abspath(path)
        if os.path.isdir(path):
            dirs.append(path)
            for f in scan_folder(path):
                if f not in seen:
                    seen.add(f)
                    files.append(f)
        elif os.path.isfile(path):
            dirs.append(os.path.dirname(path))
            if path not in seen:
                seen.add(path)
                files.append(path)
    if dirs:
        try:
            base_dir = os.path.commonpath(dirs)
            if not os.path.isdir(base_dir):
                base_dir = dirs[0]
        except ValueError:
            base_dir = dirs[0]
    else:
        base_dir = os.getcwd()
    return files, base_dir


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def emit(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    def say(text):
        if not args.json and not args.quiet:
            print(text, flush=True)

    missing = [p for p in args.paths if not os.path.exists(p)]
    for m in missing:
        print(f"docprep-core: path not found: {m}", file=sys.stderr)

    files, base_dir = collect_files([p for p in args.paths if os.path.exists(p)])
    if not files:
        print("docprep-core: no supported files found", file=sys.stderr)
        return 2

    custom_prompt = None
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            custom_prompt = f.read()
    custom_terms = None
    if args.terms_file:
        with open(args.terms_file, "r", encoding="utf-8") as f:
            custom_terms = f.read()

    signal.signal(signal.SIGINT, _handle_sigint)
    started = time.time()

    if args.json:
        emit({"event": "start", "total": len(files), "base_dir": base_dir})
    else:
        say(f"Converting {len(files)} file{'s' if len(files) != 1 else ''}...")

    stats = {"done": 0, "errors": 0, "tokens": 0, "src": 0, "out_comparable": 0}

    def progress(event):
        stats["done"] += 1
        if event["status"] == "error":
            stats["errors"] += 1
        else:
            stats["tokens"] += event["tokens"]
            if event.get("source_tokens"):
                stats["src"] += event["source_tokens"]
                stats["out_comparable"] += event["tokens"]
        if args.json:
            emit({"event": "file", **event})
        elif event["status"] == "error":
            say(f"[{event['done']}/{event['total']}] FAILED {event['file']}: {event['error']}")
        else:
            say(f"[{event['done']}/{event['total']}] {event['output']} (~{event['tokens']:,} tokens)")

    outputs = convert_files(
        files,
        progress_callback=progress,
        cancel_check=lambda: _cancelled,
        overwrite=args.conflict == "overwrite",
        inject_yaml=not args.no_yaml,
        redact_pii=args.redact,
        redact_mode=ENGINE_NAMES[args.engine],
        ollama_model=args.ollama_model,
        custom_prompt=custom_prompt,
        custom_terms=custom_terms,
    )

    combined_path = None
    total_tokens = stats["tokens"]
    if not _cancelled and args.output in ("both", "combined-only") and len(outputs) >= 2:
        name = os.path.basename(base_dir.rstrip(os.sep)) or "Documents"
        combined_name = args.combined_name or f"{name}-combined.md"
        target = os.path.join(base_dir, combined_name)
        try:
            combined_path = combine_files(
                sorted(outputs), target, base_dir=base_dir,
                overwrite=args.conflict == "overwrite",
                generate_toc=not args.no_toc,
                inject_yaml=not args.no_yaml,
                collection_name=name,
            )
            with open(combined_path, "r", encoding="utf-8") as f:
                combined_tokens = estimate_tokens(f.read())
            if args.output == "combined-only":
                for path in outputs:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                outputs = [combined_path]
                total_tokens = combined_tokens
            else:
                outputs.append(combined_path)
            if args.json:
                emit({"event": "combined", "output": combined_path, "tokens": combined_tokens})
            else:
                say(f"Combined: {combined_path} (~{combined_tokens:,} tokens)")
        except Exception as e:
            print(f"docprep-core: combine failed: {e}", file=sys.stderr)

    elapsed = round(time.time() - started, 2)
    saved = max(0, stats["src"] - stats["out_comparable"])
    summary = {
        "event": "summary",
        "converted": stats["done"] - stats["errors"],
        "failed": stats["errors"],
        "tokens": total_tokens,
        "saved_vs_raw": saved,
        "elapsed": elapsed,
        "cancelled": _cancelled,
        "combined": combined_path,
        "outputs": outputs,
    }
    if args.json:
        emit(summary)
    else:
        status = "Cancelled" if _cancelled else "Done"
        line = f"{status}: {summary['converted']} converted, {summary['failed']} failed, ~{total_tokens:,} tokens in {elapsed}s"
        if saved:
            line += f" (~{saved:,} tokens saved vs raw source)"
        print(line, flush=True)

    if _cancelled:
        return 130
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
