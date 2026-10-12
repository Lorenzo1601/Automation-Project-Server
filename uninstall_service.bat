@echo off
rem  Rimuove il servizio Automation Project Server (i dati non vengono toccati).
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set "EXE=%~dp0AutomationProjectServer.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\AutomationProjectServer.exe"
if not exist "%EXE%" (
    echo [ERRORE] AutomationProjectServer.exe non trovato.
    pause & exit /b 1
)
start "" /wait "%EXE%" remove
pause
