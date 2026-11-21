$ErrorActionPreference = "Stop"

Write-Host "Cleaning previous build artifacts..." -ForegroundColor Cyan
Remove-Item -Recurse -Force build -ErrorAction SilentlyContinue
Remove-Item -Force dist\PearlSplit.exe -ErrorAction SilentlyContinue

Write-Host "Building PearlSplit.exe with PyInstaller..." -ForegroundColor Cyan
pyinstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name "PearlSplit" `
  --icon "assets\pearl.ico" `
  --add-data "pearl_split\web;pearl_split\web" `
  --add-data "pearl_split\srb_parser.py;pearl_split" `
  --add-data "pearl_split\alert_engine.py;pearl_split" `
  --add-data "pearl_split\collector_server.py;pearl_split" `
  --add-data "assets;assets" `
  --hidden-import "pystray._win32" `
  --hidden-import "clr" `
  --hidden-import "pythonnet" `
  --hidden-import "bottle" `
  --exclude-module "numpy" `
  --exclude-module "cryptography" `
  --exclude-module "bcrypt" `
  --exclude-module "paramiko" `
  --exclude-module "nacl" `
  --exclude-module "tkinter" `
  main.py

Write-Host "Build completed: dist\PearlSplit.exe" -ForegroundColor Green
