# Builds the docprep-core sidecar for the Tauri app (Windows).
# Run from the repo root with the project venv present at .venv
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

& .venv\Scripts\pyinstaller.exe --onefile --noconfirm --name docprep-core `
  --distpath desktop\src-tauri\binaries `
  --collect-all markitdown --collect-all magika `
  --collect-all spacy --collect-all en_core_web_sm `
  --exclude-module customtkinter --exclude-module tkinterdnd2 --exclude-module tkinter `
  --exclude-module matplotlib --exclude-module scipy --exclude-module IPython `
  docprep_core.py

$triple = (& "$env:USERPROFILE\.cargo\bin\rustc.exe" -vV | Select-String "host: (.+)").Matches[0].Groups[1].Value
$target = "desktop\src-tauri\binaries\docprep-core-$triple.exe"
if (Test-Path $target) { Remove-Item $target -Force }
Rename-Item desktop\src-tauri\binaries\docprep-core.exe $target
Write-Host "Sidecar built: $target"
