#!/bin/bash
# Builds the docprep-core sidecar (onedir) for the Tauri app (macOS/Linux).
# Run from anywhere; expects the project venv at <repo>/.venv
#
# onedir avoids the onefile self-extraction delay on every launch: the exe and
# its _internal/ dependency folder are placed side by side in binaries/. Tauri
# ships the exe via externalBin and _internal via bundle.resources so they land
# together next to the app exe at install time.
set -e
cd "$(dirname "$0")/.."

STAGE="desktop/src-tauri/_sidecar_stage"
WORK="desktop/src-tauri/_sidecar_work"
rm -rf "$STAGE"

.venv/bin/pyinstaller --onedir --noconfirm --name docprep-core \
  --distpath "$STAGE" --workpath "$WORK" \
  --collect-all markitdown --collect-all magika \
  --collect-all spacy --collect-all en_core_web_sm \
  --exclude-module customtkinter --exclude-module tkinterdnd2 --exclude-module tkinter \
  --exclude-module matplotlib --exclude-module scipy --exclude-module IPython \
  --exclude-module speech_recognition --exclude-module pydub --exclude-module pocketsphinx \
  docprep_core.py

TRIPLE=$(rustc -vV | sed -n 's/host: //p')
BIN="desktop/src-tauri/binaries"
mkdir -p "$BIN"
rm -rf "$BIN"/docprep-core* "$BIN/_internal"

# Place the exe (triple-named for Tauri) and its deps folder side by side
mv "$STAGE/docprep-core/docprep-core" "$BIN/docprep-core-$TRIPLE"
mv "$STAGE/docprep-core/_internal" "$BIN/_internal"

rm -rf "$STAGE" "$WORK"
echo "Sidecar (onedir) built: $BIN/docprep-core-$TRIPLE + _internal/"
