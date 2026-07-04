#!/bin/bash
# Builds the docprep-core sidecar for the Tauri app (macOS/Linux).
# Run from anywhere; expects the project venv at <repo>/.venv
set -e
cd "$(dirname "$0")/.."

.venv/bin/pyinstaller --onefile --noconfirm --name docprep-core \
  --distpath desktop/src-tauri/binaries \
  --collect-all markitdown --collect-all magika \
  --collect-all spacy --collect-all en_core_web_sm \
  --exclude-module customtkinter --exclude-module tkinterdnd2 --exclude-module tkinter \
  --exclude-module matplotlib --exclude-module scipy --exclude-module IPython \
  docprep_core.py

TRIPLE=$(rustc -vV | sed -n 's/host: //p')
mv -f "desktop/src-tauri/binaries/docprep-core" "desktop/src-tauri/binaries/docprep-core-$TRIPLE"
echo "Sidecar built: desktop/src-tauri/binaries/docprep-core-$TRIPLE"
