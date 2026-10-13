@echo off
rem ====================================================================
rem  Automation Project Server - build dell'eseguibile CLIENT (Windows)
rem  Risultato: dist\AutomationProjectClient.exe  (file singolo, GUI)
rem ====================================================================
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || (where py >nul 2>nul && set "PY=py -3")
%PY% --version >nul 2>nul || (echo [ERRORE] Python non trovato nel PATH. Installa Python 3.10+ da python.org & goto :fail)

echo.
echo [1/4] Ambiente di build isolato (.venv-build-client)...
if not exist ".venv-build-client\Scripts\python.exe" (
    %PY% -m venv .venv-build-client || goto :fail
)
set "VPY=%~dp0.venv-build-client\Scripts\python.exe"

echo.
echo [2/4] Installazione dipendenze client + PyInstaller...
"%VPY%" -m pip install --upgrade pip --quiet || goto :fail
"%VPY%" -m pip install -r requirements-client.txt -r requirements-build.txt || goto :fail

echo.
echo [3/4] Icona dell'applicazione (formato compatibile con gli exe di Windows)...
set "ICON=%~dp0client\theme\assets\app.ico"
"%VPY%" "%~dp0tools\make_icon.py" || echo [AVVISO] Rigenerazione icona non riuscita: uso l'icona gia' presente.
if not exist "%ICON%" (echo [ERRORE] Icona non trovata: %ICON% & goto :fail)

echo.
echo [4/4] Creazione eseguibile (puo' richiedere alcuni minuti)...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name AutomationProjectClient ^
    --icon "%ICON%" ^
    --paths . ^
    --hidden-import cryptography.x509 ^
    --collect-data certifi ^
    --specpath build ^
    --workpath build\client ^
    --distpath dist ^
    --add-data "%~dp0client\theme\dark.qss;client\theme" ^
    --add-data "%~dp0client\theme\assets;client\theme\assets" ^
    run_client.py || goto :fail

echo.
"%VPY%" "%~dp0tools\check_exe_icon.py" "%~dp0dist\AutomationProjectClient.exe"
if errorlevel 1 goto :noicon
rem Aggiorna la cache delle icone di Esplora risorse, cosi' la nuova icona compare subito.
ie4uinit.exe -show >nul 2>nul
goto :icon_done
:noicon
echo [AVVISO] L'icona non risulta incorporata: controlla il file app.ico e rilancia la build.
:icon_done

echo.
echo ====================================================================
echo  OK: dist\AutomationProjectClient.exe
echo.
echo  Copia l'exe sui PC dei programmatori: al primo avvio chiede
echo  indirizzo del server (es. http://NOME-SERVER:8750), utente e password.
echo  La cartella di lavoro e' %%USERPROFILE%%\AutomationProjectClient
echo ====================================================================
pause
exit /b 0

:fail
echo.
echo [ERRORE] Build del client non riuscita.
pause
exit /b 1
