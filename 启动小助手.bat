@echo off
rem Launch XXT assistant without a console window.
rem Errors are written to data\app.log
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" goto novenv

start "" ".venv\Scripts\pythonw.exe" -m xxt.main
exit /b 0

:novenv
echo [!] Virtual environment .venv not found.
echo     Run these commands in this folder first:
echo         python -m venv .venv
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
exit /b 1
