@echo off
rem ====================================================================
rem  Installa Automation Project Server come servizio di Windows.
rem  Mettere questo file nella stessa cartella di AutomationProjectServer.exe
rem  (o lanciarlo dalla cartella del progetto: usa dist\ se l'exe e' li').
rem ====================================================================
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo Servono i privilegi di amministratore: richiesta di autorizzazione...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "EXE=%~dp0AutomationProjectServer.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\AutomationProjectServer.exe"
if not exist "%EXE%" (
    echo [ERRORE] AutomationProjectServer.exe non trovato. Esegui prima build_server.bat.
    pause & exit /b 1
)

echo.
echo  Installazione di Automation Project Server come servizio Windows
echo  ----------------------------------------------------------------
echo  Il servizio gira come LocalSystem: la cartella dati deve essere un
echo  percorso LOCALE (es. D:\APS), non un'unita' di rete.
echo.
set "DATA=D:\APS"
set /p "DATA=Cartella dei dati [D:\APS]: "
set "PORT=8750"
set /p "PORT=Porta [8750]: "
set "NAME=%COMPUTERNAME%"
set /p "NAME=Nome mostrato ai client [%COMPUTERNAME%]: "

rem start /wait: l'exe e' un'app grafica, senza di esso cmd non ne aspetta la fine
start "" /wait "%EXE%" install --data-dir "%DATA%" --port %PORT% --name "%NAME%"
if errorlevel 1 (
    echo.
    echo [ERRORE] Installazione non riuscita.
    pause & exit /b 1
)
echo.
echo  Fatto. Il servizio parte automaticamente con il PC.
echo  Stato:    start /wait "" "%EXE%" status
echo  Registro: %DATA%\logs\server.log
pause
