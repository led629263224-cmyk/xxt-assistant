@echo off
rem Launch with a console window so you can watch the live log.
rem Closing this window stops the program.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto novenv

".venv\Scripts\python.exe" -m xxt.main %*
echo.
echo Program exited. Press any key to close.
pause >nul
exit /b 0

:novenv
echo [!] Virtual environment .venv not found. See README.md
pause
exit /b 1
