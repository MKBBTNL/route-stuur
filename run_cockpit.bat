@echo off
REM --- Route Stuur launcher: double-click to start ---
cd /d "%~dp0"
echo Running from: %CD%
if not exist "cockpit.py" (
  echo.
  echo ERROR: cockpit.py not found in %CD%
  echo This almost always means the icon/shortcut you used to launch this is
  echo stale -- pointing at an old folder from before a move/rename. Fixes:
  echo   1. Open this "route-stuur" folder in Explorer and double-click
  echo      run_cockpit.bat directly from there.
  echo   2. If you use a desktop/taskbar shortcut, right-click it -^> Properties
  echo      and check "Target" / "Start in" both point into this folder.
  echo   3. If you launched this from an already-open terminal window from
  echo      before the move, close it and open a fresh one.
  echo.
  pause
  exit /b 1
)
echo Checking dependencies (first run only)...
python -m pip install --quiet --disable-pip-version-check streamlit pandas python-docx 2>nul
echo Starting Route Stuur... a browser tab will open. Leave this window open while you work.
python -m streamlit run cockpit.py
echo.
echo Route Stuur stopped. You can close this window.
pause
