"""
Platform-aware offline speech & speaker diarization model catalog and manager.

Model URLs resolve through `HF_BASE_URL` by default and automatically switch to
a custom CDN / Cloudflare R2 mirror when `DOCPREP_MODEL_MIRROR_URL` (or
`R2_MIRROR_BASE_URL`) is configured.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from backend.audio import _subprocess_kwargs, find_audio_sidecar, probe_hardware
from backend.settings import load_settings, models_dir

# Flip this constant (or set DOCPREP_MODEL_MIRROR_URL) when moving from
# Hugging Face to Cloudflare R2 — all Windows & macOS downloads route through it.
HF_BASE_URL = "https://huggingface.co"
R2_MIRROR_BASE_URL = ""

_HEX_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def get_model_registry_base() -> str:
    env_url = os.environ.get("DOCPREP_MODEL_MIRROR_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    if R2_MIRROR_BASE_URL.strip():
        return R2_MIRROR_BASE_URL.strip().rstrip("/")
    return HF_BASE_URL.rstrip("/")


def hf_resolve_url(repo_id: str, remote_path: str, revision: str = "main", r2_key: str = "") -> str:
    base = get_model_registry_base()
    if base != HF_BASE_URL.rstrip("/") and r2_key:
        return f"{base}/{r2_key.lstrip('/')}"
    return f"{base}/{repo_id}/resolve/{revision}/{remote_path.lstrip('/')}"


MACOS_MODELS: list[dict] = [
    {
        "id": "parakeet-tdt-0.6b-v3",
        "name": "Parakeet TDT 0.6B v3",
        "engine": "fluidaudio",
        "size_mb": 490,
        "languages": "25 languages (auto-detect)",
        "license": "CC-BY-4.0 / MIT",
        "min_vram_mb": 0,
        "description": "Apple Neural Engine CoreML speech recognition + offline speaker diarization.",
        "subdirs": ["parakeet-tdt-0.6b-v3", "speaker-diarization"],
        "required_paths": [
            "parakeet-tdt-0.6b-v3/Preprocessor.mlmodelc",
            "parakeet-tdt-0.6b-v3/Encoder.mlmodelc",
            "parakeet-tdt-0.6b-v3/Decoder.mlmodelc",
            "parakeet-tdt-0.6b-v3/JointDecisionv3.mlmodelc",
            "parakeet-tdt-0.6b-v3/parakeet_vocab.json",
            "speaker-diarization/Segmentation.mlmodelc",
            "speaker-diarization/FBank.mlmodelc",
            "speaker-diarization/Embedding.mlmodelc",
            "speaker-diarization/PldaRho.mlmodelc",
            "speaker-diarization/plda-parameters.json",
        ],
    }
]

WINDOWS_DIARIZATION_FILES: list[dict] = [
    {
        "local_rel": "diarization/segmentation.int8.onnx",
        "repo_id": "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0",
        "remote_path": "model.int8.onnx",
        "r2_key": "windows/diarization/segmentation.int8.onnx",
        "sha256": None,
        "approx_bytes": 1_600_000,
    },
    {
        "local_rel": "diarization/embedding.onnx",
        "repo_id": "csukuangfj/speaker-embedding-models",
        "remote_path": "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
        "r2_key": "windows/diarization/embedding.onnx",
        "sha256": None,
        "approx_bytes": 28_300_000,
    },
]

WINDOWS_MODELS: list[dict] = [
    {
        "id": "whisper-large-v3-turbo-q8_0",
        "name": "Whisper Large v3 Turbo (Q8_0)",
        "engine": "whisper",
        "size_mb": 865,
        "languages": "99 languages (auto-detect)",
        "license": "MIT",
        "min_vram_mb": 6144,
        "description": "Highest accuracy multilingual Whisper model for GPUs with 6 GB+ VRAM (Vulkan).",
        "subdirs": ["whisper-large-v3-turbo-q8_0"],
        "files": [
            {
                "local_rel": "whisper-large-v3-turbo-q8_0/ggml-model.bin",
                "repo_id": "ggerganov/whisper.cpp",
                "remote_path": "ggml-large-v3-turbo-q8_0.bin",
                "r2_key": "windows/whisper-large-v3-turbo-q8_0/ggml-model.bin",
                "sha256": "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1",
                "approx_bytes": 874_188_075,
            }
        ],
    },
    {
        "id": "whisper-large-v3-turbo-q5_0",
        "name": "Whisper Large v3 Turbo (Q5_0)",
        "engine": "whisper",
        "size_mb": 578,
        "languages": "99 languages (auto-detect)",
        "license": "MIT",
        "min_vram_mb": 4096,
        "description": "Balanced multilingual Whisper model for GPUs with 4-6 GB VRAM (Vulkan).",
        "subdirs": ["whisper-large-v3-turbo-q5_0"],
        "files": [
            {
                "local_rel": "whisper-large-v3-turbo-q5_0/ggml-model.bin",
                "repo_id": "ggerganov/whisper.cpp",
                "remote_path": "ggml-large-v3-turbo-q5_0.bin",
                "r2_key": "windows/whisper-large-v3-turbo-q5_0/ggml-model.bin",
                "sha256": "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
                "approx_bytes": 574_041_195,
            }
        ],
    },
    {
        "id": "parakeet-tdt-0.6b-v3-int8",
        "name": "Parakeet TDT 0.6B v3 (INT8 CPU)",
        "engine": "sherpa-parakeet",
        "size_mb": 668,
        "languages": "25 languages (auto-detect)",
        "license": "CC-BY-4.0 / Apache-2.0",
        "min_vram_mb": 0,
        "description": "Fastest CPU speech model on laptops/desktops without a discrete GPU.",
        "subdirs": ["parakeet-tdt-0.6b-v3-int8"],
        "files": [
            {
                "local_rel": "parakeet-tdt-0.6b-v3-int8/encoder.int8.onnx",
                "repo_id": "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                "remote_path": "encoder.int8.onnx",
                "r2_key": "windows/parakeet-tdt-0.6b-v3-int8/encoder.int8.onnx",
                "sha256": None,
                "approx_bytes": 652_000_000,
            },
            {
                "local_rel": "parakeet-tdt-0.6b-v3-int8/decoder.int8.onnx",
                "repo_id": "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                "remote_path": "decoder.int8.onnx",
                "r2_key": "windows/parakeet-tdt-0.6b-v3-int8/decoder.int8.onnx",
                "sha256": None,
                "approx_bytes": 11_900_000,
            },
            {
                "local_rel": "parakeet-tdt-0.6b-v3-int8/joiner.int8.onnx",
                "repo_id": "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                "remote_path": "joiner.int8.onnx",
                "r2_key": "windows/parakeet-tdt-0.6b-v3-int8/joiner.int8.onnx",
                "sha256": None,
                "approx_bytes": 6_400_000,
            },
            {
                "local_rel": "parakeet-tdt-0.6b-v3-int8/tokens.txt",
                "repo_id": "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                "remote_path": "tokens.txt",
                "r2_key": "windows/parakeet-tdt-0.6b-v3-int8/tokens.txt",
                "sha256": None,
                "approx_bytes": 95_000,
            },
        ],
    },
    {
        "id": "whisper-small-q5_1",
        "name": "Whisper Small (Q5_1 Compact)",
        "engine": "whisper",
        "size_mb": 212,
        "languages": "99 languages (auto-detect)",
        "license": "MIT",
        "min_vram_mb": 0,
        "description": "Lightweight 99-language fallback for low-RAM or metered connections.",
        "subdirs": ["whisper-small-q5_1"],
        "files": [
            {
                "local_rel": "whisper-small-q5_1/ggml-model.bin",
                "repo_id": "ggerganov/whisper.cpp",
                "remote_path": "ggml-small-q5_1.bin",
                "r2_key": "windows/whisper-small-q5_1/ggml-model.bin",
                "sha256": None,
                "approx_bytes": 190_085_487,
            }
        ],
    },
]


def get_platform_catalog(platform_override: Optional[str] = None) -> list[dict]:
    plat = platform_override or sys.platform
    if plat == "darwin":
        return MACOS_MODELS
    return WINDOWS_MODELS


def get_model_spec(model_id: str, platform_override: Optional[str] = None) -> Optional[dict]:
    for entry in get_platform_catalog(platform_override):
        if entry["id"] == model_id:
            return entry
    return None


def recommend_model(hw_info: Optional[dict] = None) -> str:
    hw = hw_info or probe_hardware()
    os_name = str(hw.get("os") or "").lower()
    if os_name == "macos" or (not os_name and sys.platform == "darwin"):
        return "parakeet-tdt-0.6b-v3"

    vulkan = bool(hw.get("vulkan"))
    vram_mb = int(hw.get("vram_mb") or 0)
    ram_mb = int(hw.get("ram_mb") or 8192)

    if vulkan and vram_mb >= 6144:
        return "whisper-large-v3-turbo-q8_0"
    if vulkan and vram_mb >= 4096:
        return "whisper-large-v3-turbo-q5_0"
    if ram_mb < 6144:
        return "whisper-small-q5_1"
    return "parakeet-tdt-0.6b-v3-int8"


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _, files in os.walk(path):
        for fname in files:
            if fname.endswith(".part"):
                continue
            try:
                total += (Path(root) / fname).stat().st_size
            except OSError:
                pass
    return total


def is_diarization_installed(root: Optional[Path | str] = None) -> bool:
    base = Path(root or models_dir())
    if sys.platform == "darwin":
        return all(
            (base / rel).exists()
            for rel in (
                "speaker-diarization/Segmentation.mlmodelc",
                "speaker-diarization/FBank.mlmodelc",
                "speaker-diarization/Embedding.mlmodelc",
                "speaker-diarization/PldaRho.mlmodelc",
                "speaker-diarization/plda-parameters.json",
            )
        )
    return all(
        (base / f["local_rel"]).is_file() and (base / f["local_rel"]).stat().st_size > 0
        for f in WINDOWS_DIARIZATION_FILES
    )


def is_model_installed(model_id: str, root: Optional[Path | str] = None) -> bool:
    spec = get_model_spec(model_id)
    if not spec:
        return False
    base = Path(root or models_dir())

    if spec["engine"] == "fluidaudio":
        return all((base / rel).exists() for rel in spec["required_paths"])

    for f in spec.get("files", []):
        target = base / f["local_rel"]
        if not target.is_file():
            return False
        try:
            if target.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return is_diarization_installed(base)


def model_disk_usage(model_id: str, root: Optional[Path | str] = None) -> int:
    spec = get_model_spec(model_id)
    if not spec:
        return 0
    base = Path(root or models_dir())
    return sum(_dir_size_bytes(base / sub) for sub in spec.get("subdirs", []))


def resolve_active_model_id(preferred: str = "") -> str:
    """Return the first usable installed model (preferring `preferred` -> saved setting -> recommended)."""
    root = Path(models_dir())
    candidates: list[str] = []
    if preferred and preferred.strip():
        candidates.append(preferred.strip())
    saved = str(load_settings().get("audio_model") or "").strip()
    if saved and saved not in candidates:
        candidates.append(saved)

    for cid in candidates:
        if is_model_installed(cid, root=root):
            return cid

    for entry in get_platform_catalog():
        if is_model_installed(entry["id"], root=root):
            return entry["id"]

    return ""


def list_models(hw_info: Optional[dict] = None) -> dict:
    hw = hw_info or probe_hardware()
    rec_id = recommend_model(hw)
    root = Path(models_dir())
    active_id = resolve_active_model_id()

    items: list[dict] = []
    for entry in get_platform_catalog():
        mid = entry["id"]
        installed = is_model_installed(mid, root=root)
        items.append(
            {
                "id": mid,
                "name": entry["name"],
                "engine": entry["engine"],
                "size_mb": entry["size_mb"],
                "languages": entry["languages"],
                "license": entry["license"],
                "min_vram_mb": entry["min_vram_mb"],
                "description": entry["description"],
                "recommended": mid == rec_id,
                "installed": installed,
                "active": mid == active_id,
                "disk_bytes": model_disk_usage(mid, root=root) if installed else 0,
            }
        )

    return {
        "hardware": hw,
        "recommended_model": rec_id,
        "active_model": active_id,
        "diarization_installed": is_diarization_installed(root),
        "models_dir": str(root),
        "models": items,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _download_single_http_file(
    url: str,
    dest_path: Path,
    expected_sha256: Optional[str],
    on_bytes: Callable[[int, int], None],
) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_name(dest_path.name + ".part")
    resume_offset = part_path.stat().st_size if part_path.is_file() else 0

    headers = {"User-Agent": "AIDocPrep/2.1"}
    if resume_offset > 0:
        headers["Range"] = f"bytes={resume_offset}-"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        status = getattr(resp, "status", 200)
        if status == 200 and resume_offset > 0:
            # Server ignored Range; restart cleanly
            resume_offset = 0

        content_len_str = resp.headers.get("Content-Length")
        rem_bytes = int(content_len_str) if content_len_str and content_len_str.isdigit() else 0
        total_bytes = (resume_offset + rem_bytes) if rem_bytes > 0 else 0

        linked_etag = (resp.headers.get("X-Linked-ETag") or "").strip().strip('"')
        effective_sha = expected_sha256
        if not effective_sha and _HEX_SHA256_RE.match(linked_etag):
            effective_sha = linked_etag.lower()

        mode = "ab" if (status == 206 and resume_offset > 0) else "wb"
        downloaded = resume_offset if mode == "ab" else 0
        on_bytes(downloaded, total_bytes)

        with open(part_path, mode) as out_fp:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out_fp.write(chunk)
                downloaded += len(chunk)
                on_bytes(downloaded, total_bytes)

    if effective_sha:
        actual_sha = _sha256_file(part_path).lower()
        if actual_sha != effective_sha.lower():
            try:
                part_path.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"SHA-256 verification failed for {dest_path.name} "
                f"(expected {effective_sha[:12]}..., got {actual_sha[:12]}...)"
            )

    os.replace(part_path, dest_path)


def download_model(
    model_id: str,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Download `model_id` (+ companion diarization weights) with live progress."""
    spec = get_model_spec(model_id)
    if not spec:
        raise ValueError(f"Unknown model ID for this platform: {model_id!r}")

    root = Path(models_dir())
    root.mkdir(parents=True, exist_ok=True)

    def emit(stage: str, pct: int, **extra: object) -> None:
        if progress_cb is not None:
            payload = {
                "event": "download",
                "model_id": model_id,
                "stage": stage,
                "pct": max(0, min(100, int(pct))),
                **extra,
            }
            progress_cb(payload)

    if spec["engine"] == "fluidaudio":
        sidecar = find_audio_sidecar()
        if not sidecar:
            raise RuntimeError(
                "macOS audio sidecar ('docprep-audio') was not found. "
                "Run desktop/build-sidecar.sh first."
            )
        env = os.environ.copy()
        mirror = get_model_registry_base()
        if mirror != HF_BASE_URL.rstrip("/"):
            env["DOCPREP_MODEL_MIRROR_URL"] = mirror

        proc = subprocess.Popen(
            [
                sidecar,
                "--download",
                "--models-root",
                str(root),
                "--model-id",
                model_id,
                "--with-diarizer",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **_subprocess_kwargs(),
        )
        err_msg: Optional[str] = None
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
                if msg.get("event") == "download":
                    emit(str(msg.get("stage") or "asr"), int(msg.get("pct") or 0))
                elif msg.get("event") == "error":
                    err_msg = str(msg.get("message") or "Model download failed")
            stderr_out = proc.stderr.read().strip() if proc.stderr else ""
            rc = proc.wait()
        except BaseException:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise

        if err_msg or rc != 0:
            raise RuntimeError(err_msg or stderr_out or f"Model download exited with code {rc}")

        if not is_model_installed(model_id, root=root):
            raise RuntimeError("Model download finished, but expected CoreML files were not found on disk.")

        emit("complete", 100)
        return {
            "model_id": model_id,
            "installed": True,
            "disk_bytes": model_disk_usage(model_id, root=root),
        }

    # Windows / HTTP multi-file download with Range resume + SHA-256 verification
    queue: list[tuple[str, dict]] = []
    for f in spec.get("files", []):
        if not (root / f["local_rel"]).is_file():
            queue.append(("asr", f))
    if not is_diarization_installed(root):
        for f in WINDOWS_DIARIZATION_FILES:
            if not (root / f["local_rel"]).is_file():
                queue.append(("diarizer", f))

    if not queue:
        emit("complete", 100)
        return {
            "model_id": model_id,
            "installed": True,
            "disk_bytes": model_disk_usage(model_id, root=root),
        }

    grand_total = sum(max(1, int(item[1].get("approx_bytes") or 1)) for item in queue)
    completed_prior = 0
    last_pct = -1

    for stage, f in queue:
        url = hf_resolve_url(
            f["repo_id"],
            f["remote_path"],
            revision=f.get("revision", "main"),
            r2_key=f.get("r2_key", ""),
        )
        dest = root / f["local_rel"]
        est_file = max(1, int(f.get("approx_bytes") or 1))

        def on_file_bytes(done_b: int, total_b: int) -> None:
            nonlocal last_pct
            denom = grand_total
            effective_done = completed_prior + min(done_b, total_b or est_file)
            pct = min(99, int(round(effective_done * 100.0 / max(1, denom))))
            if pct > last_pct:
                last_pct = pct
                emit(stage, pct, downloaded_bytes=effective_done, total_bytes=grand_total)

        _download_single_http_file(url, dest, f.get("sha256"), on_file_bytes)
        completed_prior += est_file

    emit("complete", 100, downloaded_bytes=grand_total, total_bytes=grand_total)
    return {
        "model_id": model_id,
        "installed": True,
        "disk_bytes": model_disk_usage(model_id, root=root),
    }


def delete_model(model_id: str) -> dict:
    """Remove `model_id` from disk (and clean up shared diarization if no speech models remain)."""
    spec = get_model_spec(model_id)
    if not spec:
        raise ValueError(f"Unknown model ID for this platform: {model_id!r}")

    root = Path(models_dir())
    primary_subdir = spec["subdirs"][0]
    target = root / primary_subdir
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    # Remove shared diarization directory if zero speech models remain installed
    any_remaining = False
    for other in get_platform_catalog():
        if other["id"] == model_id:
            continue
        other_dir = root / other["subdirs"][0]
        if other_dir.exists():
            any_remaining = True
            break

    if not any_remaining:
        for shared_sub in ("speaker-diarization", "speaker-diarization-offline", "diarization"):
            shared_path = root / shared_sub
            if shared_path.exists():
                shutil.rmtree(shared_path, ignore_errors=True)

    return {"model_id": model_id, "deleted": True}
