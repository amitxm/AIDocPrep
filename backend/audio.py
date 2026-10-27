"""
Local offline audio/video transcription & speaker diarization bridge.

Delegates hardware probing, media duration probing, and ASR + diarization
inference to the native platform sidecar (`docprep-audio`), serializes
GPU / Apple Neural Engine access across worker threads, and supports
instant per-file cancellation without aborting batch runs.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

MEDIA_AUDIO_EXTENSIONS = [
    ".mp3",
    ".m4a",
    ".wav",
    ".flac",
    ".ogg",
    ".aac",
    ".opus",
]

MEDIA_VIDEO_EXTENSIONS = [
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
]

MEDIA_EXTENSIONS = MEDIA_AUDIO_EXTENSIONS + MEDIA_VIDEO_EXTENSIONS

# Pause threshold (seconds) above which consecutive segments by the same
# speaker start a fresh timestamped paragraph.
SAME_SPEAKER_MAX_GAP_SEC = 3.0


class AudioModelMissingError(RuntimeError):
    """Raised when transcription is requested before downloading a speech model."""


class AudioNoTrackError(RuntimeError):
    """Raised when a video/container file contains no decodable audio stream."""


class AudioSkippedError(RuntimeError):
    """Raised when a queued or in-flight media file is skipped by the user."""


# Serializes heavy GPU / Neural Engine inference to one active media file at a time
# while allowing non-media document workers in ThreadPoolExecutor to continue.
_transcribe_lock = threading.Lock()

# Tracks the currently executing sidecar child process and user-skipped files.
_state_lock = threading.Lock()
_active_proc: Optional[subprocess.Popen] = None
_active_file: Optional[str] = None
_cancelled_files: set[str] = set()


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def reset_cancelled_files() -> None:
    """Clear the per-batch skipped-file set at the start of a new CLI/UI run."""
    with _state_lock:
        _cancelled_files.clear()


def is_file_cancelled(file_path: str) -> bool:
    norm = _norm_path(file_path)
    with _state_lock:
        return norm in _cancelled_files


def cancel_audio_file(file_path: str) -> bool:
    """Mark `file_path` as skipped and terminate its `docprep-audio` process if active."""
    norm = _norm_path(file_path)
    proc_to_kill: Optional[subprocess.Popen] = None
    with _state_lock:
        _cancelled_files.add(norm)
        if _active_file == norm and _active_proc is not None:
            proc_to_kill = _active_proc

    if proc_to_kill is not None:
        _terminate_process_tree(proc_to_kill)
        return True
    return False


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Stop the sidecar and anything it spawned.

    On Windows, terminate() is TerminateProcess on the direct child only. If
    that child is a launcher (a .cmd wrapper, or any helper it started), the
    grandchild survives holding our stdout pipe, and the reader thread blocks
    until it exits on its own. taskkill /T walks the tree.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=5,
            )
        except Exception:
            pass
    try:
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError:
        pass


def _platform_triples() -> list[str]:
    mach = platform.machine().lower()
    if sys.platform == "darwin":
        if mach in ("arm64", "aarch64"):
            return ["aarch64-apple-darwin", "x86_64-apple-darwin"]
        return ["x86_64-apple-darwin", "aarch64-apple-darwin"]
    if sys.platform == "win32":
        if mach in ("arm64", "aarch64"):
            return ["aarch64-pc-windows-msvc", "x86_64-pc-windows-msvc"]
        return ["x86_64-pc-windows-msvc"]
    return [f"{mach}-unknown-linux-gnu"]


def find_audio_sidecar() -> Optional[str]:
    """Locate the native `docprep-audio` executable across bundled and dev layouts."""
    env_override = os.environ.get("DOCPREP_AUDIO_BIN", "").strip()
    if env_override and os.path.isfile(env_override):
        return env_override

    is_win = sys.platform == "win32"
    ext = ".exe" if is_win else ""
    triples = _platform_triples()

    candidates: list[Path] = []

    # 1. Bundled app directory alongside frozen `docprep-core` (`Contents/MacOS/` or Windows install dir)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / f"docprep-audio{ext}")
        for t in triples:
            candidates.append(exe_dir / f"docprep-audio-{t}{ext}")

    # 2. Repo development locations
    repo_root = Path(__file__).resolve().parent.parent
    bin_dir = repo_root / "desktop" / "src-tauri" / "binaries"
    for t in triples:
        candidates.append(bin_dir / f"docprep-audio-{t}{ext}")
    candidates.append(bin_dir / f"docprep-audio{ext}")

    if sys.platform == "darwin":
        candidates.append(
            repo_root / "audio-sidecar" / "macos" / ".build" / "release" / "docprep-audio"
        )
    elif is_win:
        candidates.append(
            repo_root / "audio-sidecar" / "windows" / "target" / "release" / "docprep-audio.exe"
        )

    for candidate in candidates:
        if candidate.is_file() and (is_win or os.access(candidate, os.X_OK)):
            return str(candidate)

    return None


def _subprocess_kwargs() -> dict:
    """Hide transient console windows on Windows GUI builds."""
    kwargs: dict = {}
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = creationflags
    return kwargs


def probe_hardware() -> dict:
    """Return hardware capabilities dict from `docprep-audio --hardware` (with OS fallback)."""
    sidecar = find_audio_sidecar()
    if sidecar:
        try:
            proc = subprocess.run(
                [sidecar, "--hardware"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                **_subprocess_kwargs(),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        return json.loads(line)
        except Exception:
            pass

    # Fallback when sidecar binary has not been compiled yet
    os_name = "macos" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else sys.platform)
    mach = platform.machine().lower()
    arch = "aarch64" if mach in ("arm64", "aarch64") else ("x86_64" if mach in ("amd64", "x86_64") else mach)
    return {
        "os": os_name,
        "arch": arch,
        "chip_or_gpu": platform.processor() or f"{os_name} ({arch})",
        "gpu_vendor": "apple" if sys.platform == "darwin" else "unknown",
        "vram_mb": 0,
        "ram_mb": 8192,
        "vulkan": False,
        "avx2": arch == "x86_64",
    }


def probe_media_files(paths: list[str]) -> list[dict]:
    """Return `[{"path": ..., "duration_sec": ..., "has_audio": ...}]` via `docprep-audio --probe`."""
    if not paths:
        return []
    sidecar = find_audio_sidecar()
    if not sidecar:
        return [{"path": p, "duration_sec": 0.0, "has_audio": True} for p in paths]
    try:
        proc = subprocess.run(
            [sidecar, "--probe", *paths],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **_subprocess_kwargs(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip())
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return [{"path": p, "duration_sec": 0.0, "has_audio": True} for p in paths]


def format_timestamp(seconds: float) -> str:
    """Format seconds into `HH:MM:SS` (`00:01:14`)."""
    total = max(0, int(round(seconds)))
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def format_duration_human(seconds: float) -> str:
    """Format duration for YAML frontmatter / UI summaries (`48m 12s`, `1h 04m 09s`)."""
    total = max(0, int(round(seconds)))
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hrs > 0:
        return f"{hrs}h {mins:02d}m {secs:02d}s"
    if mins > 0:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def merge_speaker_segments(
    segments: list[dict],
    max_gap_sec: float = SAME_SPEAKER_MAX_GAP_SEC,
) -> list[dict]:
    """Merge consecutive turns by the same speaker separated by <= `max_gap_sec`."""
    merged: list[dict] = []
    for raw in segments:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        start = float(raw.get("start") or 0.0)
        end = max(start, float(raw.get("end") or start))
        speaker = raw.get("speaker")
        speaker_str = str(speaker).strip() if speaker else None

        if merged:
            prev = merged[-1]
            gap = start - prev["end"]
            if (
                speaker_str is not None
                and prev["speaker"] == speaker_str
                and gap <= max_gap_sec
            ):
                prev["end"] = max(prev["end"], end)
                prev["text"] = f"{prev['text']} {text}"
                continue

        merged.append(
            {
                "start": start,
                "end": end,
                "speaker": speaker_str,
                "text": text,
            }
        )
    return merged


def format_transcript_markdown(
    title: str,
    segments: list[dict],
    max_gap_sec: float = SAME_SPEAKER_MAX_GAP_SEC,
) -> str:
    """Render diarized or undiarized transcript segments into clean Markdown."""
    paragraphs = merge_speaker_segments(segments, max_gap_sec=max_gap_sec)
    lines = [f"# {title}", ""]

    if not paragraphs:
        lines.append("*(No speech detected in audio track.)*")
        return "\n".join(lines) + "\n"

    for seg in paragraphs:
        ts = format_timestamp(seg["start"])
        speaker = seg.get("speaker")
        if speaker:
            lines.append(f"**[{ts}] {speaker}:** {seg['text']}")
        else:
            lines.append(f"**[{ts}]** {seg['text']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def transcribe_media_file(
    file_path: str,
    *,
    model_id: str = "",
    diarize: bool = True,
    speakers: str = "auto",
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> dict:
    """
    Run offline transcription (+ optional diarization) on `file_path`.

    Returns:
        {
            "markdown": str,
            "duration_sec": float,
            "duration_human": str,
            "speakers": int,
            "model_id": str,
            "segments": list[dict],
        }
    """
    from backend.models import resolve_active_model_id
    from backend.settings import models_dir

    if is_file_cancelled(file_path):
        raise AudioSkippedError("Skipped by user")

    sidecar = find_audio_sidecar()
    if not sidecar:
        raise RuntimeError(
            "Native audio sidecar ('docprep-audio') was not found. "
            "Build it via desktop/build-sidecar.sh (macOS) or desktop/build-sidecar.ps1 (Windows)."
        )

    effective_model = resolve_active_model_id(preferred=model_id)
    if not effective_model:
        raise AudioModelMissingError(
            "No offline speech model is installed. Open Settings -> Audio & Video to download a model, "
            "or run with --download-model."
        )

    norm_target = _norm_path(file_path)

    with _transcribe_lock:
        if is_file_cancelled(file_path):
            raise AudioSkippedError("Skipped by user")

        cmd = [
            sidecar,
            "--transcribe",
            os.path.abspath(file_path),
            "--models-root",
            str(models_dir()),
            "--model-id",
            effective_model,
        ]
        if diarize:
            cmd.append("--diarize")
            cmd.extend(["--speakers", str(speakers or "auto")])
        else:
            cmd.append("--no-diarize")

        global _active_proc, _active_file
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_subprocess_kwargs(),
        )
        with _state_lock:
            _active_proc = proc
            _active_file = norm_target
            already_cancelled = norm_target in _cancelled_files

        if already_cancelled:
            proc.terminate()

        result_payload: Optional[dict] = None
        error_payload: Optional[dict] = None

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ev = msg.get("event")
                if ev == "progress":
                    if progress_cb is not None:
                        stage = str(msg.get("stage") or "transcribing")
                        pct = int(msg.get("pct") or 0)
                        progress_cb(stage, pct)
                elif ev == "result":
                    result_payload = msg
                elif ev == "error":
                    error_payload = msg

            stderr_text = ""
            if proc.stderr is not None:
                stderr_text = proc.stderr.read().strip()
            rc = proc.wait()
        finally:
            with _state_lock:
                if _active_proc is proc:
                    _active_proc = None
                    _active_file = None

        if is_file_cancelled(file_path):
            raise AudioSkippedError("Skipped by user")

        if error_payload is not None:
            code = error_payload.get("code", "")
            message = error_payload.get("message") or "Audio transcription failed"
            if code == "model_missing":
                raise AudioModelMissingError(message)
            if code == "no_audio" or rc == 3:
                raise AudioNoTrackError(message)
            raise RuntimeError(message)

        if rc != 0 or result_payload is None:
            detail = stderr_text or f"docprep-audio exited with code {rc}"
            raise RuntimeError(f"Audio transcription failed: {detail}")

        duration_sec = float(result_payload.get("duration_sec") or 0.0)
        speaker_count = int(result_payload.get("speakers") or 0)
        segments = result_payload.get("segments") or []
        title = Path(file_path).stem
        markdown = format_transcript_markdown(title, segments)

        return {
            "markdown": markdown,
            "duration_sec": duration_sec,
            "duration_human": format_duration_human(duration_sec),
            "speakers": speaker_count,
            "model_id": effective_model,
            "segments": segments,
        }
