@echo off
rem Builds dist\RaspberryFlash.exe (single file, no console, requests admin rights)
rem and dist\ManifestMaker.exe (single file, no console)
cd /d "%~dp0"
python -m pip install --quiet pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name RaspberryFlash raspberry_flash.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name ManifestMaker manifest_maker.py
