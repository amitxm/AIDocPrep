# Builds the docprep-core sidecar (onedir) for the Tauri app (Windows).
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
  --collect-all spacy --collect-all en_core_web_sm `
  --exclude-module customtkinter --exclude-module tkinterdnd2 --exclude-module tkinter `
  --exclude-module matplotlib --exclude-module scipy --exclude-module IPython `
  --exclude-module speech_recognition --exclude-module pydub --exclude-module pocketsphinx `
  docprep_core.py

$triple = (& "$env:USERPROFILE\.cargo\bin\rustc.exe" -vV | Select-String "host: (.+)").Matches[0].Groups[1].Value.Trim()

$bin = "desktop\src-tauri\binaries"
New-Item -ItemType Directory -Force $bin | Out-Null
# Clear any previous sidecar payload
Get-ChildItem $bin -Filter "docprep-core*" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
if (Test-Path "$bin\_internal") { Remove-Item "$bin\_internal" -Recurse -Force }

# Place the exe (triple-named for Tauri) and its deps folder side by side
Move-Item "$stage\docprep-core\docprep-core.exe" "$bin\docprep-core-$triple.exe"
Move-Item "$stage\docprep-core\_internal" "$bin\_internal"

Remove-Item $stage -Recurse -Force
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Sidecar (onedir) built: $bin\docprep-core-$triple.exe + _internal\"
