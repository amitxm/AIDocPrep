Write-Host "Building Windows Executable (Folder Mode for Installer)..."
pyinstaller -y --noconsole --name AIDocPrep --icon icon.ico --collect-data customtkinter --collect-data tkinterdnd2 --collect-all magika app.py

Write-Host "Copying LICENSE..."
Copy-Item "LICENSE" "dist\AIDocPrep\LICENSE" -Force

Write-Host "Build Complete! Next, compile installer.iss using Inno Setup."
