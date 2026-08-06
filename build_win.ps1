Write-Host "Building Windows Executable (Folder Mode for Installer)..."
pyinstaller -y --noconsole --name AIDocPrep --icon icon.ico --exclude-module speech_recognition --exclude-module pocketsphinx --exclude-module pydub --exclude-module matplotlib --exclude-module scipy --exclude-module IPython --exclude-module tkinter.test --exclude-module unittest --exclude-module pydoc --collect-all customtkinter --collect-all tkinterdnd2 --collect-all magika --hidden-import olefile --hidden-import xlrd --collect-all spacy --collect-all en_core_web_sm app.py

Write-Host "Copying LICENSE..."
Copy-Item "LICENSE" "dist\AIDocPrep\LICENSE" -Force

Write-Host "Build Complete! Next, compile installer.iss using Inno Setup."
