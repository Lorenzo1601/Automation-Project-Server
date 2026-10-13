@echo off
rem ====================================================================
rem  Automation Project Server - build dell'eseguibile SERVER (Windows)
rem  Risultato:  dist\AutomationProjectServer.exe   (UN SOLO exe)
rem    - senza argomenti: applicazione grafica (utenti, cartella dati, rete)
rem    - install / start / stop / remove / status: servizio Windows
rem    - serve: server da riga di comando senza interfaccia
rem ====================================================================
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || (where py >nul 2>nul && set "PY=py -3")
%PY% --version >nul 2>nul || (echo [ERRORE] Python non trovato nel PATH. Installa Python 3.10+ da python.org & goto :fail)

echo.
echo [1/4] Ambiente di build isolato (.venv-build-server)...
if not exist ".venv-build-server\Scripts\python.exe" (
    %PY% -m venv .venv-build-server || goto :fail
)
set "VPY=%~dp0.venv-build-server\Scripts\python.exe"

echo.
echo [2/4] Installazione dipendenze (server, interfaccia grafica, servizio) + PyInstaller...
"%VPY%" -m pip install --upgrade pip --quiet || goto :fail
"%VPY%" -m pip install -r requirements-server.txt -r requirements-client.txt -r requirements-service.txt -r requirements-build.txt || goto :fail

echo.
echo [3/4] Icona dell'applicazione (formato compatibile con gli exe di Windows)...
set "ICON=%~dp0client\theme\assets\app.ico"
"%VPY%" "%~dp0tools\make_icon.py" || echo [AVVISO] Rigenerazione icona non riuscita: uso l'icona gia' presente.
if not exist "%ICON%" (echo [ERRORE] Icona non trovata: %ICON% & goto :fail)

echo.
echo [4/4] Creazione eseguibile (puo' richiedere alcuni minuti)...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name AutomationProjectServer ^
    --icon "%ICON%" ^
    --paths . ^
    --specpath build ^
    --workpath build\server ^
    --distpath dist ^
    --collect-submodules uvicorn ^
    --collect-submodules websockets ^
    --hidden-import sqlalchemy.dialects.sqlite.pysqlite ^
    --hidden-import python_multipart ^
    --hidden-import multipart ^
    --hidden-import jwt ^
    --hidden-import bcrypt ^
    --collect-submodules cryptography ^
    --collect-data certifi ^
    --hidden-import starlette.testclient ^
    --hidden-import win32timezone ^
    --hidden-import servicemanager ^
    --hidden-import win32serviceutil ^
    --hidden-import win32service ^
    --hidden-import win32event ^
    --hidden-import win32api ^
    --hidden-import win32con ^
    --add-data "%~dp0client\theme\dark.qss;client\theme" ^
    --add-data "%~dp0client\theme\assets;client\theme\assets" ^
    run_server.py || goto :fail

echo.
"%VPY%" "%~dp0tools\check_exe_icon.py" "%~dp0dist\AutomationProjectServer.exe"
if errorlevel 1 goto :noicon
rem Aggiorna la cache delle icone di Esplora risorse, cosi' la nuova icona compare subito.
ie4uinit.exe -show >nul 2>nul
goto :icon_done
:noicon
echo [AVVISO] L'icona non risulta incorporata: controlla il file app.ico e rilancia la build.
:icon_done

echo.
echo ====================================================================
echo  OK: dist\AutomationProjectServer.exe   (un solo file)
echo.
echo  Doppio clic: applicazione grafica. Al primo avvio scegli la cartella
echo  dati, l'amministratore e la porta; gli utenti si creano da
echo  "Utenti e Progetti". I client trovano il server da soli in rete.
echo.
echo  Servizio Windows (parte con il PC senza aprire l'app): dalle
echo  Impostazioni dell'app, oppure copia l'exe e install_service.bat sul PC
echo  del server ed esegui install_service.bat (chiede i privilegi di
echo  amministratore e apre il firewall: TCP 8750, UDP 8751).
echo.
echo  Da terminale: start /wait "" AutomationProjectServer.exe status
echo                AutomationProjectServer.exe serve --help   (senza interfaccia)
echo ====================================================================
pause
exit /b 0

:fail
echo.
echo [ERRORE] Build del server non riuscita.
pause
exit /b 1
