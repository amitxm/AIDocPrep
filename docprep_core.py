"""docprep-core — headless CLI for the AI DocPrep conversion engine.

Converts office documents and audio/video recordings to Markdown from the
command line, and doubles as the machine-readable engine for GUI shells: with
--json it emits one JSON event per line on stdout (start, file, combined,
summary), so any front end (or script) can stream progress.

Examples:
  python docprep_core.py report.docx slides.pptx meeting.m4a
  python docprep_core.py ./research --output combined-only --redact
  python docprep_core.py --list-models
  python docprep_core.py --download-model parakeet-tdt-0.6b-v3
  python docprep_core.py ./docs --json > events.jsonl
"""
import os
import sys
import json
import time
import signal
import warnings
import argparse

# pydub (pulled in by markitdown for audio) warns about missing ffmpeg at
# import time; irrelevant for document conversion and noisy on stderr
warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv")

from backend.converter import convert_files, scan_folder, estimate_tokens
from backend.combiner import combine_files
from backend.audio import cancel_audio_file, probe_hardware, reset_cancelled_files
from backend.models import delete_model, download_model, list_models

VERSION = "1.3.0"

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
        description=(
            "Convert documents (.docx, .pdf, .pptx, .xlsx, .xls, .msg, .epub, .ipynb, .html, .vtt) "
            "and audio/video (.mp3, .m4a, .wav, .flac, .ogg, .aac, .opus, .mp4, .mov, .webm, .mkv) "
            "to clean Markdown."
        ),
    )
    p.add_argument("paths", nargs="*", help="files and/or folders to convert")
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
    p.add_argument("--prompt", default=None, metavar="TEXT",
                   help="custom Ollama redaction prompt")
    p.add_argument("--prompt-file", default=None, metavar="PATH",
                   help="file containing a custom Ollama redaction prompt")
    p.add_argument("--term", action="append", default=None, metavar="TERM",
                   help="custom term to always redact (repeatable)")
    p.add_argument("--terms-file", default=None, metavar="PATH",
                   help="file with custom terms to redact, one per line")
    p.add_argument("--ocr", action="store_true",
                   help="OCR scanned PDFs and images embedded in documents (slow; off by default)")
    p.add_argument("--allow-zip", action="store_true",
                   help="convert ZIP archives (contents filtered to supported formats, size-capped)")
    p.add_argument("--manifest", default=None, metavar="PATH",
                   help="write a JSON record of which output each file went to")
    p.add_argument("--replace-from", default=None, metavar="PATH",
                   help="re-run in place: overwrite the outputs recorded in a --manifest file instead of adding copies")
    p.add_argument("--audio-model", default="", metavar="ID",
                   help="speech model ID for audio/video transcription (default: active/recommended installed model)")
    p.add_argument("--no-diarize", action="store_true",
                   help="disable speaker diarization for audio/video files")
    p.add_argument("--speakers", default="auto", metavar="COUNT",
                   help="speaker count hint for diarization: auto, 2, 3, or 4+ (default: auto)")
    p.add_argument("--exclude", action="append", default=None, metavar="PATH",
                   help="specific file path to skip (repeatable)")
    p.add_argument("--hardware-json", action="store_true",
                   help="print detected CPU/GPU/VRAM capabilities as JSON and exit")
    p.add_argument("--list-models", action="store_true",
                   help="list available and installed offline transcription models and exit")
    p.add_argument("--download-model", default=None, metavar="ID",
                   help="download an offline transcription model (+ diarization weights) and exit")
    p.add_argument("--delete-model", default=None, metavar="ID",
                   help="delete an installed offline transcription model and exit")
    p.add_argument("--json", action="store_true", help="emit JSON-lines events on stdout")
    p.add_argument("--watchdog", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--quiet", action="store_true", help="only print the final summary (human mode)")
    p.add_argument("--version", action="version", version=f"docprep-core {VERSION}")
    return p


def collect_files(paths: list[str], excludes: list[str] = None) -> tuple[list[str], str]:
    """Expands folders, filters excluded paths, dedupes, and returns (files, common_base_dir)."""
    excluded_norm = {
        os.path.normcase(os.path.abspath(p))
        for p in (excludes or [])
    }
    files = []
    seen = set()
    dirs = []
    for path in paths:
        path = os.path.abspath(path)
        if os.path.normcase(path) in excluded_norm:
            continue
        if os.path.isdir(path):
            dirs.append(path)
            for f in scan_folder(path):
                norm_f = os.path.normcase(os.path.abspath(f))
                if norm_f in excluded_norm:
                    continue
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


def load_manifest(path: str) -> tuple[dict, str | None]:
    """Reads a --manifest record: ({source: output}, combined output or None).
    Only .md targets are honoured, so a damaged record can't aim a re-run at
    anything but a Markdown file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def is_md(p):
        return isinstance(p, str) and p.lower().endswith(".md")

    outputs = {src: dest for src, dest in (data.get("outputs") or {}).items() if is_md(dest)}
    combined = data.get("combined")
    return outputs, (combined if is_md(combined) else None)


def write_manifest(path: str, outputs: dict, combined: str | None) -> str | None:
    """Writes the record a later --replace-from run reads. Returns the path, or
    None if it couldn't be written (the caller then offers no in-place re-run)."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "outputs": outputs, "combined": combined}, f, ensure_ascii=False)
        os.replace(tmp, path)
        return path
    except OSError as e:
        print(f"docprep-core: could not write manifest: {e}", file=sys.stderr)
        return None


def _start_watchdog_thread() -> None:
    # GUI shells pass --watchdog: exit when stdin reaches EOF, and accept
    # live JSON commands (`{"cmd": "skip", "file": "..."}`) while running.
    import threading
    from backend import audio as audio_mod

    def _watch_stdin():
        try:
            for raw in sys.stdin.buffer:
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("{"):
                    try:
                        msg = json.loads(line)
                        if msg.get("cmd") == "skip" and msg.get("file"):
                            cancel_audio_file(str(msg["file"]))
                    except Exception:
                        pass
        except Exception:
            pass

        with audio_mod._state_lock:
            proc = audio_mod._active_proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        os._exit(130)

    threading.Thread(target=_watch_stdin, daemon=True).start()


def _handle_management_commands(args, emit) -> int:
    if args.hardware_json:
        emit(probe_hardware())
        return 0

    if args.list_models:
        data = list_models()
        if args.json:
            emit(data)
        else:
            hw = data["hardware"]
            print(f"Hardware: {hw.get('chip_or_gpu')} (RAM: {hw.get('ram_mb')} MB, VRAM: {hw.get('vram_mb')} MB)")
            print(f"Models directory: {data['models_dir']}\n")
            for m in data["models"]:
                badges = []
                if m["installed"]:
                    badges.append("installed")
                if m["recommended"]:
                    badges.append("recommended")
                badge_str = f" [{', '.join(badges)}]" if badges else ""
                print(f"  {m['id']}{badge_str}")
                print(f"    {m['name']} — ~{m['size_mb']} MB — {m['languages']}")
                print(f"    {m['description']}")
        return 0

    if args.download_model:
        if args.watchdog:
            _start_watchdog_thread()
        model_id = args.download_model.strip()

        def on_dl(ev: dict):
            if args.json:
                emit(ev)
            elif not args.quiet:
                print(f"\rDownloading {model_id} ({ev['stage']}): {ev['pct']}%", end="", flush=True)

        try:
            res = download_model(model_id, progress_cb=on_dl)
            if args.json:
                emit({"event": "download_complete", **res})
            else:
                if not args.quiet:
                    print()
                mb = round(res.get("disk_bytes", 0) / (1024 * 1024), 1)
                print(f"Installed {model_id} ({mb} MB)")
            return 0
        except Exception as e:
            if args.json:
                emit({"event": "error", "message": str(e)})
            else:
                print(f"\ndocprep-core: download failed: {e}", file=sys.stderr)
            return 1

    if args.delete_model:
        model_id = args.delete_model.strip()
        try:
            res = delete_model(model_id)
            if args.json:
                emit({"event": "model_deleted", **res})
            else:
                print(f"Deleted {model_id}")
            return 0
        except Exception as e:
            if args.json:
                emit({"event": "error", "message": str(e)})
            else:
                print(f"docprep-core: delete failed: {e}", file=sys.stderr)
            return 1

    return -1


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def emit(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    def say(text):
        if not args.json and not args.quiet:
            print(text, flush=True)

    mgmt_rc = _handle_management_commands(args, emit)
    if mgmt_rc >= 0:
        return mgmt_rc

    if not args.paths:
        parser.error("the following arguments are required: paths")

    reset_cancelled_files()

    missing = [p for p in args.paths if not os.path.exists(p)]
    for m in missing:
        print(f"docprep-core: path not found: {m}", file=sys.stderr)

    files, base_dir = collect_files([p for p in args.paths if os.path.exists(p)], excludes=args.exclude)
    if not files:
        print("docprep-core: no supported files found", file=sys.stderr)
        return 2

    custom_prompt = args.prompt
    if custom_prompt is None and args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            custom_prompt = f.read()
    custom_terms = "\n".join(args.term) if args.term else None
    if custom_terms is None and args.terms_file:
        with open(args.terms_file, "r", encoding="utf-8") as f:
            custom_terms = f.read()

    replace_outputs, replace_combined = {}, None
    if args.replace_from:
        try:
            replace_outputs, replace_combined = load_manifest(args.replace_from)
        except (OSError, ValueError) as e:
            print(f"docprep-core: cannot read --replace-from manifest: {e}", file=sys.stderr)
            return 2

    signal.signal(signal.SIGINT, _handle_sigint)

    if args.watchdog:
        _start_watchdog_thread()

    started = time.time()

    if args.json:
        emit({"event": "start", "total": len(files), "base_dir": base_dir})
    else:
        say(f"Converting {len(files)} file{'s' if len(files) != 1 else ''}...")

    stats = {"done": 0, "errors": 0, "skipped": 0, "tokens": 0, "src": 0, "out_comparable": 0}
    written = {}  # source -> output, for --manifest

    def progress(event):
        if event["status"] in ("started", "progress"):
            if args.json:
                emit({"event": "file", **event})
            return
        stats["done"] += 1
        if event["status"] == "error":
            stats["errors"] += 1
        elif event["status"] == "skipped":
            stats["skipped"] += 1
        else:
            written[event["file"]] = event["output"]
            stats["tokens"] += event["tokens"]
            if event.get("source_tokens"):
                stats["src"] += event["source_tokens"]
                stats["out_comparable"] += event["tokens"]
        if args.json:
            emit({"event": "file", **event})
        elif event["status"] == "error":
            say(f"[{event['done']}/{event['total']}] FAILED {event['file']}: {event['error']}")
        elif event["status"] == "skipped":
            say(f"[{event['done']}/{event['total']}] SKIPPED {event['file']}")
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
        allow_zip=args.allow_zip,
        ocr=args.ocr,
        output_paths=replace_outputs,
        diarize=not args.no_diarize,
        speakers=args.speakers,
        audio_model=args.audio_model,
    )

    combined_path = None
    total_tokens = stats["tokens"]
    if not _cancelled and args.output in ("both", "combined-only") and len(outputs) >= 2:
        name = os.path.basename(base_dir.rstrip(os.sep)) or "Documents"
        if replace_combined:
            target, overwrite_combined = replace_combined, True
        else:
            combined_name = args.combined_name or f"{name}-combined.md"
            target = os.path.join(base_dir, combined_name)
            overwrite_combined = args.conflict == "overwrite"
        try:
            combined_path = combine_files(
                sorted(outputs), target, base_dir=base_dir,
                overwrite=overwrite_combined,
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

    manifest = write_manifest(args.manifest, written, combined_path) if args.manifest else None

    elapsed = round(time.time() - started, 2)
    saved = max(0, stats["src"] - stats["out_comparable"])
    converted_count = max(0, stats["done"] - stats["errors"] - stats["skipped"])
    summary = {
        "event": "summary",
        "converted": converted_count,
        "failed": stats["errors"],
        "skipped": stats["skipped"],
        "tokens": total_tokens,
        "saved_vs_raw": saved,
        "src_tokens": stats["src"],
        "out_comparable": stats["out_comparable"],
        "elapsed": elapsed,
        "cancelled": _cancelled,
        "combined": combined_path,
        "outputs": outputs,
        "manifest": manifest,
    }
    if args.json:
        emit(summary)
    else:
        status = "Cancelled" if _cancelled else "Done"
        line = f"{status}: {summary['converted']} converted, {summary['failed']} failed"
        if summary["skipped"]:
            line += f", {summary['skipped']} skipped"
        line += f", ~{total_tokens:,} tokens in {elapsed}s"
        if stats["src"] > stats["out_comparable"] > 0:
            pct = min(99, round(100 * (1 - stats["out_comparable"] / stats["src"])))
            if pct > 0:
                line += f" ({pct}% saved vs raw)"
        print(line, flush=True)

    if _cancelled:
        return 130
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
