@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Could not install requirements.
  pause
  exit /b 1
)
set "TELETHON_CONNECTION_MODE=obfuscated"
python gen_session.py
pause
