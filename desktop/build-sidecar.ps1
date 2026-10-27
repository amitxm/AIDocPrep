# Builds the docprep-core (onedir) and docprep-audio (native Rust) sidecars for the Tauri app (Windows).
# Run from the repo root with the project venv present at .venv
#
# onedir avoids the onefile self-extraction delay on every launch: the exe and
# its _internal/ dependency folder are placed side by side in binaries/. Tauri
# ships the exe via externalBin and _internal via bundle.resources so they land
# together next to the app exe at install time.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$stage = "desktop\src-tauri\_sidecar_stage"
$work = "desktop\src-tauri\_sidecar_work"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }

& .venv\Scripts\pyinstaller.exe --onedir --noconfirm --name docprep-core `
  --distpath $stage --workpath $work `
  --collect-all markitdown --collect-all magika `
  --hidden-import olefile --hidden-import xlrd `
  --collect-all rapidocr_onnxruntime --collect-all pypdfium2 `
  --collect-all spacy --collect-all en_core_web_sm `
  --exclude-module customtkinter --exclude-module tkinterdnd2 --exclude-module tkinter `
  --exclude-module matplotlib --exclude-module scipy --exclude-module IPython `
  --exclude-module speech_recognition --exclude-module pydub --exclude-module pocketsphinx `
  docprep_core.py

$cargoBin = if (Test-Path "$env:USERPROFILE\.cargo\bin\rustc.exe") { "$env:USERPROFILE\.cargo\bin" } else { "" }
$rustcExe = if ($cargoBin) { "$cargoBin\rustc.exe" } else { "rustc.exe" }
$cargoExe = if ($cargoBin) { "$cargoBin\cargo.exe" } else { "cargo.exe" }

$triple = (& $rustcExe -vV | Select-String "host: (.+)").Matches[0].Groups[1].Value.Trim()

$bin = "desktop\src-tauri\binaries"
New-Item -ItemType Directory -Force $bin | Out-Null
# Clear any previous sidecar payload
Get-ChildItem $bin -Filter "docprep-core*" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Get-ChildItem $bin -Filter "docprep-audio*" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
if (Test-Path "$bin\_internal") { Remove-Item "$bin\_internal" -Recurse -Force }

# Place the exe (triple-named for Tauri) and its deps folder side by side
Move-Item "$stage\docprep-core\docprep-core.exe" "$bin\docprep-core-$triple.exe"
Move-Item "$stage\docprep-core\_internal" "$bin\_internal"

# opencv-python ships a ~30 MB ffmpeg DLL for video capture that OCR never
# loads (verified: cv2 imports and RapidOCR runs without it). Drop it.
Get-ChildItem "$bin\_internal\cv2" -Filter "opencv_videoio_ffmpeg*.dll" -ErrorAction SilentlyContinue | Remove-Item -Force

Remove-Item $stage -Recurse -Force
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Sidecar (onedir) built: $bin\docprep-core-$triple.exe + _internal\"

# ---------------------------------------------------------------------------
# Build Windows native audio sidecar (docprep-audio) + stage inference runtime
# ---------------------------------------------------------------------------
Write-Host "Building Windows native audio sidecar (docprep-audio)..."
& $cargoExe build --release --manifest-path "audio-sidecar\windows\Cargo.toml"
if ($LASTEXITCODE -ne 0) {
    throw "cargo build failed for audio-sidecar\windows"
}

Copy-Item "audio-sidecar\windows\target\release\docprep-audio.exe" "$bin\docprep-audio-$triple.exe" -Force
Write-Host "Audio sidecar built: $bin\docprep-audio-$triple.exe"

$audioRuntimeDir = "$bin\_internal\audio_runtime"
New-Item -ItemType Directory -Force $audioRuntimeDir | Out-Null

$vendorCache = "audio-sidecar\windows\.vendor_cache"
$cachedRuntime = "$vendorCache\runtime"
New-Item -ItemType Directory -Force $cachedRuntime | Out-Null

if ($env:DOCPREP_SKIP_AUDIO_VENDOR -ne "1") {
    # 1. Stage whisper.cpp Windows x64 runtime (whisper-cli.exe + ggml/whisper DLLs)
    if (-not (Test-Path "$cachedRuntime\whisper-cli.exe")) {
        $whisperZip = "$vendorCache\whisper-bin-x64.zip"
        $whisperExtract = "$vendorCache\whisper_extract"
        $whisperUrl = if ($env:DOCPREP_WHISPER_WIN_URL) {
            $env:DOCPREP_WHISPER_WIN_URL
        } else {
            "https://github.com/ggml-org/whisper.cpp/releases/download/v1.7.6/whisper-bin-x64.zip"
        }
        Write-Host "Downloading whisper.cpp Windows runtime from $whisperUrl ..."
        Invoke-WebRequest -Uri $whisperUrl -OutFile $whisperZip -UseBasicParsing
        if (Test-Path $whisperExtract) { Remove-Item $whisperExtract -Recurse -Force }
        Expand-Archive -Path $whisperZip -DestinationPath $whisperExtract -Force

        Get-ChildItem -Path $whisperExtract -Recurse -Include "whisper-cli.exe", "main.exe", "*.dll" | ForEach-Object {
            Copy-Item $_.FullName -Destination $cachedRuntime -Force
        }
        if ((-not (Test-Path "$cachedRuntime\whisper-cli.exe")) -and (Test-Path "$cachedRuntime\main.exe")) {
            Copy-Item "$cachedRuntime\main.exe" "$cachedRuntime\whisper-cli.exe" -Force
        }
        Remove-Item $whisperExtract -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 2. Stage sherpa-onnx Windows x64 shared C-API + offline diarization runtime
    if (-not (Test-Path "$cachedRuntime\sherpa-onnx-c-api.dll")) {
        $sherpaArchive = "$vendorCache\sherpa-onnx-win-x64.tar.bz2"
        $sherpaExtract = "$vendorCache\sherpa_extract"
        $sherpaUrl = if ($env:DOCPREP_SHERPA_WIN_URL) {
            $env:DOCPREP_SHERPA_WIN_URL
        } else {
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.12.28/sherpa-onnx-v1.12.28-win-x64-shared-MT-Release-no-tts.tar.bz2"
        }
        Write-Host "Downloading sherpa-onnx Windows runtime from $sherpaUrl ..."
        Invoke-WebRequest -Uri $sherpaUrl -OutFile $sherpaArchive -UseBasicParsing
        if (Test-Path $sherpaExtract) { Remove-Item $sherpaExtract -Recurse -Force }
        New-Item -ItemType Directory -Force $sherpaExtract | Out-Null
        & tar.exe -xjf $sherpaArchive -C $sherpaExtract
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to extract $sherpaArchive via tar.exe"
        }

        Get-ChildItem -Path $sherpaExtract -Recurse -Include `
            "sherpa-onnx-c-api.dll", `
            "sherpa-onnx-cxx-api.dll", `
            "onnxruntime*.dll", `
            "sherpa-onnx-offline.exe", `
            "sherpa-onnx-offline-speaker-diarization.exe" | ForEach-Object {
            Copy-Item $_.FullName -Destination $cachedRuntime -Force
        }
        Remove-Item $sherpaExtract -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path $cachedRuntime) {
    Get-ChildItem -Path $cachedRuntime -File | ForEach-Object {
        Copy-Item $_.FullName -Destination $audioRuntimeDir -Force
    }
    Write-Host "Staged Windows audio inference runtime in: $audioRuntimeDir"
}
