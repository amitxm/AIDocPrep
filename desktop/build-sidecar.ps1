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
# Build the Windows native audio sidecar (docprep-audio): whisper.cpp with the
# ggml Vulkan backend + sherpa-onnx, all in-process (see audio-sidecar/windows).
#
# One-time machine prerequisites:
#   - Rust (stable-msvc)            winget install Rustlang.Rustup
#   - VS 2022 Build Tools with the C++ workload (cl.exe, CMake, Ninja)
#   - LLVM (libclang for bindgen)   winget install LLVM.LLVM
#   - Vulkan SDK (shader compiler)  winget install KhronosGroup.VulkanSDK
#
# Two rules learned the hard way:
#   1. cargo must run inside the MSVC developer environment (vcvars64), or
#      CMake cannot find cl.exe for whisper.cpp.
#   2. The cargo target dir must be SHORT. whisper.cpp's Vulkan shader
#      generator nests a CMake project ~150 characters below the crate's build
#      dir; from the repo path that exceeds MAX_PATH and fails with
#      "error C1083: Cannot open compiler generated file". Default: C:\dpa
#      (override with DOCPREP_AUDIO_TARGET_DIR).
# ---------------------------------------------------------------------------
Write-Host "Building Windows native audio sidecar (docprep-audio)..."

$vsRoot = (& "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1)
if (-not $vsRoot) { throw "Visual Studio 2022 Build Tools with the C++ workload were not found (vswhere)." }
$vcvars = Join-Path $vsRoot "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found under $vsRoot" }
$vsCMake = Join-Path $vsRoot "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin"
$vsNinja = Join-Path $vsRoot "Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja"

if (-not $env:VULKAN_SDK) {
    $sdk = Get-ChildItem "C:\VulkanSDK" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($sdk) { $env:VULKAN_SDK = $sdk.FullName }
}
if (-not $env:VULKAN_SDK -or -not (Test-Path "$env:VULKAN_SDK\Bin\glslc.exe")) {
    throw "Vulkan SDK not found. Install it (winget install KhronosGroup.VulkanSDK) or set VULKAN_SDK."
}
if (-not $env:LIBCLANG_PATH) {
    if (Test-Path "C:\Program Files\LLVM\bin\libclang.dll") { $env:LIBCLANG_PATH = "C:\Program Files\LLVM\bin" }
}
if (-not $env:LIBCLANG_PATH -or -not (Test-Path "$env:LIBCLANG_PATH\libclang.dll")) {
    throw "libclang not found. Install LLVM (winget install LLVM.LLVM) or set LIBCLANG_PATH."
}

$audioTarget = if ($env:DOCPREP_AUDIO_TARGET_DIR) { $env:DOCPREP_AUDIO_TARGET_DIR } else { "C:\dpa" }
New-Item -ItemType Directory -Force $audioTarget | Out-Null
$env:CARGO_TARGET_DIR = $audioTarget
$env:CMAKE_GENERATOR = "Ninja"
$env:PATH = "$cargoBin;$env:VULKAN_SDK\Bin;$env:LIBCLANG_PATH;$vsCMake;$vsNinja;$env:PATH"

$manifest = (Resolve-Path "audio-sidecar\windows\Cargo.toml").Path
cmd /c "`"$vcvars`" >nul 2>&1 && `"$cargoExe`" build --release --manifest-path `"$manifest`""
if ($LASTEXITCODE -ne 0) {
    throw "cargo build failed for audio-sidecar\windows (see output above)"
}
Remove-Item Env:\CARGO_TARGET_DIR -ErrorAction SilentlyContinue
Remove-Item Env:\CMAKE_GENERATOR -ErrorAction SilentlyContinue

$audioExe = Join-Path $audioTarget "release\docprep-audio.exe"
if (-not (Test-Path $audioExe)) { throw "Expected $audioExe after cargo build" }
Copy-Item $audioExe "$bin\docprep-audio-$triple.exe" -Force

# sherpa-onnx is linked dynamically: its runtime DLLs must sit beside the exe.
# They are copied next to the triple-named sidecar here (dev layout) and
# tauri.windows.conf.json maps binaries/*.dll into the install root, which is
# where Tauri also places the renamed docprep-audio.exe.
$runtimeDlls = @("onnxruntime.dll", "onnxruntime_providers_shared.dll", "sherpa-onnx-c-api.dll", "sherpa-onnx-cxx-api.dll", "cargs.dll")
Get-ChildItem $bin -Filter "*.dll" -ErrorAction SilentlyContinue | Remove-Item -Force
foreach ($dll in $runtimeDlls) {
    $src = Join-Path $audioTarget "release\$dll"
    if (-not (Test-Path $src)) { throw "Runtime DLL missing from cargo output: $src" }
    Copy-Item $src (Join-Path $bin $dll) -Force
}
$dllMb = [math]::Round((Get-ChildItem $bin -Filter "*.dll" | Measure-Object Length -Sum).Sum / 1MB, 1)
$exeMb = [math]::Round((Get-Item "$bin\docprep-audio-$triple.exe").Length / 1MB, 1)
Write-Host "Audio sidecar built: $bin\docprep-audio-$triple.exe ($exeMb MB) + $($runtimeDlls.Count) runtime DLLs ($dllMb MB)"
