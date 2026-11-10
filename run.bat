@echo off
rem mdFOAM Droplet Viewer launcher: creates .venv on first run, then opens the app in the browser.
rem Options are passed through, e.g.  run.bat --port 8800  /  run.bat --no-browser
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Setting up the Python environment ^(first run only, needs internet^)...
  py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist .venv\Scripts\python.exe (
    echo.
    echo Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH" in the installer^), then run this file again.
    pause
    exit /b 1
  )
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -e ".[mcp]"
  if errorlevel 1 (
    echo Package installation failed. Delete the .venv folder and run this file again.
    pause
    exit /b 1
  )
)
.venv\Scripts\python -m mdfoam_droplet_viewer %*
if errorlevel 1 pause
