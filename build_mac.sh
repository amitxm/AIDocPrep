#!/bin/bash
echo "Building macOS App Bundle..."
pyinstaller -y --windowed --name AIDocPrep --exclude-module speech_recognition --exclude-module pocketsphinx --exclude-module pydub --exclude-module matplotlib --exclude-module scipy --exclude-module IPython --exclude-module tkinter.test --exclude-module unittest --exclude-module pydoc --collect-data customtkinter --collect-data tkinterdnd2 --collect-all magika app.py

echo "Copying LICENSE..."
cp LICENSE dist/AIDocPrep.app/Contents/Resources/LICENSE

echo "Build Complete! Check the 'dist' folder."
