@echo off
:: TIMPAL — Windows auto-start setup
:: Adds TIMPAL node to Task Scheduler so it starts automatically on login.

set SCRIPT=%cd%\timpal.py
set PYTHON=python

echo.
echo   Setting up TIMPAL auto-start...

schtasks /create /tn "TIMPAL Node" /tr "%PYTHON% %SCRIPT%" /sc onlogon /rl limited /f >nul 2>&1

if %errorlevel% == 0 (
    echo.
    echo   TIMPAL node will now start automatically on login.
    echo   To stop auto-start:
    echo   schtasks /delete /tn "TIMPAL Node" /f
) else (
    echo.
    echo   Setup failed. Try running this script as Administrator.
)
echo.
pause
