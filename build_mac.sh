#!/bin/bash
echo "Generating macOS icon bundle..."
if [ -f icon.png ]; then
    mkdir -p icon.iconset
    sips -z 16 16     icon.png --out icon.iconset/icon_16x16.png > /dev/null 2>&1
    sips -z 32 32     icon.png --out icon.iconset/icon_16x16@2x.png > /dev/null 2>&1
    sips -z 32 32     icon.png --out icon.iconset/icon_32x32.png > /dev/null 2>&1
    sips -z 64 64     icon.png --out icon.iconset/icon_32x32@2x.png > /dev/null 2>&1
    sips -z 128 128   icon.png --out icon.iconset/icon_128x128.png > /dev/null 2>&1
    sips -z 256 256   icon.png --out icon.iconset/icon_128x128@2x.png > /dev/null 2>&1
    sips -z 256 256   icon.png --out icon.iconset/icon_256x256.png > /dev/null 2>&1
    sips -z 512 512   icon.png --out icon.iconset/icon_256x256@2x.png > /dev/null 2>&1
    sips -z 512 512   icon.png --out icon.iconset/icon_512x512.png > /dev/null 2>&1
    sips -z 1024 1024 icon.png --out icon.iconset/icon_512x512@2x.png > /dev/null 2>&1
    iconutil -c icns icon.iconset
    rm -rf icon.iconset
    echo "✓ icon.icns created successfully!"
else
    echo "⚠ icon.png not found, building without custom icon."
fi

echo "Setting Tcl/Tk 8.6 library paths for PyInstaller..."
export TCL_LIBRARY="/opt/homebrew/opt/tcl-tk@8/lib/tcl8.6"
export TK_LIBRARY="/opt/homebrew/opt/tcl-tk@8/lib/tk8.6"

echo "Building macOS App Bundle..."
ICON_ARG=""
if [ -f icon.icns ]; then
    ICON_ARG="--icon icon.icns"
fi

pyinstaller -y --windowed --name AIDocPrep $ICON_ARG --add-data "/opt/homebrew/opt/tcl-tk@8/lib/tcl8.6:tcl8.6" --add-data "/opt/homebrew/opt/tcl-tk@8/lib/tk8.6:tk8.6" --exclude-module speech_recognition --exclude-module pocketsphinx --exclude-module pydub --exclude-module matplotlib --exclude-module scipy --exclude-module IPython --exclude-module tkinter.test --exclude-module unittest --exclude-module pydoc --collect-all customtkinter --collect-all tkinterdnd2 --collect-all magika --hidden-import olefile --hidden-import xlrd --collect-all rapidocr_onnxruntime --collect-all pypdfium2 --collect-all spacy --collect-all en_core_web_sm app.py

echo "Copying LICENSE..."
cp LICENSE dist/AIDocPrep.app/Contents/Resources/LICENSE

echo "Build Complete! Check the 'dist' folder."
