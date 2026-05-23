#!/bin/bash
echo "Building macOS App Bundle..."
pyinstaller -y --windowed --name AIDocPrep --collect-data customtkinter --collect-data tkinterdnd2 --collect-all magika app.py

echo "Copying LICENSE..."
cp LICENSE dist/AIDocPrep.app/Contents/Resources/LICENSE

echo "Build Complete! Check the 'dist' folder."
