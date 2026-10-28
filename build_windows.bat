@echo off
REM Compila SaveSync per Windows: serve Python 3.10+ (python.org, con "Add to PATH").
REM Risultato: dist\SaveSync\SaveSync.exe  (+ installer se c'e' Inno Setup 6)
cd /d "%~dp0"
python -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller pytest || goto :error
python -m pytest -q tests || goto :error
pyinstaller --noconfirm savesync.spec || goto :error
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% installer.iss || goto :error
) else (
  echo Inno Setup non trovato: salto l'installer. Il programma e' in dist\SaveSync
)
echo.
echo Fatto! Programma: dist\SaveSync\SaveSync.exe
exit /b 0
:error
echo Build fallita.
exit /b 1
