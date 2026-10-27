#!/bin/bash
# Builds the docprep-core sidecar for the Tauri app (macOS/Linux).
# Run from anywhere; expects the project venv at <repo>/.venv
set -e
cd "$(dirname "$0")/.."

STAGE="desktop/src-tauri/_sidecar_stage"
WORK="desktop/src-tauri/_sidecar_work"
rm -rf "$STAGE" "$WORK"

TRIPLE=$(rustc -vV | sed -n 's/host: //p')
BIN="desktop/src-tauri/binaries"
mkdir -p "$BIN"

# Check platform
OS_NAME=$(uname -s)

if [ "$OS_NAME" = "Darwin" ]; then
  echo "Building macOS sidecar (onefile layout)..."
  
  # Setup code-signing arguments if APPLE_SIGNING_IDENTITY is provided
  CODESIGN_ARGS=()
  if [ -n "$APPLE_SIGNING_IDENTITY" ]; then
    echo "✓ Using codesign identity: $APPLE_SIGNING_IDENTITY"
    CODESIGN_ARGS+=(
      "--codesign-identity" "$APPLE_SIGNING_IDENTITY"
      "--osx-entitlements-file" "desktop/src-tauri/entitlements.plist"
    )
  else
    echo "⚠ APPLE_SIGNING_IDENTITY not set. Building unsigned sidecar."
  fi

  .venv/bin/pyinstaller --onefile --noconfirm --name docprep-core \
    --distpath "$STAGE" --workpath "$WORK" \
    "${CODESIGN_ARGS[@]}" \
    --collect-all markitdown --collect-all magika \
    --hidden-import olefile --hidden-import xlrd \
    --collect-all rapidocr_onnxruntime --collect-all pypdfium2 \
    --collect-all spacy --collect-all en_core_web_sm \
    --exclude-module customtkinter --exclude-module tkinterdnd2 --exclude-module tkinter \
    --exclude-module matplotlib --exclude-module scipy --exclude-module IPython \
    --exclude-module speech_recognition --exclude-module pydub --exclude-module pocketsphinx \
    docprep_core.py

  rm -rf "$BIN"/docprep-core*
  mv "$STAGE/docprep-core" "$BIN/docprep-core-$TRIPLE"
  echo "Sidecar (onefile) built and placed at: $BIN/docprep-core-$TRIPLE"

  echo "Building macOS native audio sidecar (FluidAudio CoreML)..."
  swift build --package-path audio-sidecar/macos -c release
  rm -rf "$BIN"/docprep-audio*
  cp "audio-sidecar/macos/.build/release/docprep-audio" "$BIN/docprep-audio-$TRIPLE"
  chmod +x "$BIN/docprep-audio-$TRIPLE"
  strip -x "$BIN/docprep-audio-$TRIPLE" 2>/dev/null || true

  if [ -n "$APPLE_SIGNING_IDENTITY" ]; then
    codesign --force --options runtime --timestamp \
      --entitlements "desktop/src-tauri/entitlements.plist" \
      --sign "$APPLE_SIGNING_IDENTITY" \
      "$BIN/docprep-audio-$TRIPLE"
    echo "✓ Signed audio sidecar: $BIN/docprep-audio-$TRIPLE"
  fi
  echo "Audio sidecar built and placed at: $BIN/docprep-audio-$TRIPLE"

else
  echo "Building Linux sidecar (onedir layout)..."
  .venv/bin/pyinstaller --onedir --noconfirm --name docprep-core \
    --distpath "$STAGE" --workpath "$WORK" \
    --collect-all markitdown --collect-all magika \
    --hidden-import olefile --hidden-import xlrd \
    --collect-all rapidocr_onnxruntime --collect-all pypdfium2 \
    --collect-all spacy --collect-all en_core_web_sm \
    --exclude-module customtkinter --exclude-module tkinterdnd2 --exclude-module tkinter \
    --exclude-module matplotlib --exclude-module scipy --exclude-module IPython \
    --exclude-module speech_recognition --exclude-module pydub --exclude-module pocketsphinx \
    docprep_core.py

  rm -rf "$BIN"/docprep-core* "$BIN/_internal"
  mv "$STAGE/docprep-core/docprep-core" "$BIN/docprep-core-$TRIPLE"
  mv "$STAGE/docprep-core/_internal" "$BIN/_internal"
  echo "Sidecar (onedir) built: $BIN/docprep-core-$TRIPLE + _internal/"
fi

rm -rf "$STAGE" "$WORK"
