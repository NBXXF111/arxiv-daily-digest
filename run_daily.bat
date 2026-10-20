@echo off
setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "PYTHONIOENCODING=utf-8"

rem Prefer pythonw so no console window appears every morning.
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PYW set "PYW=%%i"
)
if not defined PYW (
    python "%~dp0arxiv_daily.py" >> "logs\bat.log" 2>&1
) else (
    "%PYW%" "%~dp0arxiv_daily.py" >> "logs\bat.log" 2>&1
)
exit /b %errorlevel%
