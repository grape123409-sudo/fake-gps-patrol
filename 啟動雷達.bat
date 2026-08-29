@echo off
chcp 65001 >nul
cd /d "%~dp0"
python main.py
if not errorlevel 1 goto :end
echo.
echo Program exited with an error, please check the message above.
pause
:end
