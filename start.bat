@echo off
setlocal
cd /d "%~dp0"

rem Ports/host are read by app.py from HUMANIZER_BACKEND_PORT,
rem HUMANIZER_FRONTEND_PORT and HUMANIZER_HOST (set them in your
rem environment to override the defaults). Extra arguments are
rem forwarded as-is, e.g.:  start.bat --backend-port 9001

where uv >nul 2>nul
if %errorlevel%==0 (
  uv run python app.py %*
) else (
  echo uv not found - falling back to system python
  python app.py %*
)
endlocal
