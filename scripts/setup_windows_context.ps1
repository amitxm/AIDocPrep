$executablePath = Join-Path $PSScriptRoot "..\dist\AIDocPrep\AIDocPrep.exe"

if (-Not (Test-Path $executablePath)) {
    Write-Host "Warning: Executable not found. Please build the application first."
    Write-Host "Expected path: $executablePath"
}

# Add context menu for files
$fileKeyPath = "HKCU:\Software\Classes\*\shell\Convert to Markdown"
New-Item -Path $fileKeyPath -Force | Out-Null
New-ItemProperty -Path $fileKeyPath -Name "Icon" -Value $executablePath -Force | Out-Null
$commandKeyFile = "$fileKeyPath\command"
New-Item -Path $commandKeyFile -Force | Out-Null
New-ItemProperty -Path $commandKeyFile -Name "(default)" -Value "`"$executablePath`" `"%1`"" -Force | Out-Null

# Add context menu for folders
$folderKeyPath = "HKCU:\Software\Classes\Directory\shell\Convert to Markdown"
New-Item -Path $folderKeyPath -Force | Out-Null
New-ItemProperty -Path $folderKeyPath -Name "Icon" -Value $executablePath -Force | Out-Null
$commandKeyFolder = "$folderKeyPath\command"
New-Item -Path $commandKeyFolder -Force | Out-Null
New-ItemProperty -Path $commandKeyFolder -Name "(default)" -Value "`"$executablePath`" `"%1`"" -Force | Out-Null

Write-Host "Context menu entries added successfully."
