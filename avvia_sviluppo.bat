@echo off
rem ============================================================================================
rem  PANificator 360 - avvio per lo sviluppo (doppio clic).
rem
rem  - usa l'ambiente .venv senza bisogno di attivarlo;
rem  - lavora su data\prova.sqlite: alla prima volta e' una copia di data\pmo.sqlite (o un database
rem    nuovo con i dati di config\seed.yaml), il database vero non viene toccato;
rem  - aggiorna lo schema del database (pmo init-db);
rem  - avvia il server su http://localhost:8010 con il login di sviluppo (solo email, niente Google)
rem    e lo fa ripartire da solo quando cambiano codice, template, CSS o JavaScript;
rem  - apre il browser.
rem
rem  Per fermarlo: Ctrl+C in questa finestra, oppure chiudere la finestra.
rem  Le modifiche a .env richiedono di chiudere e riaprire il batch.
rem  Porta diversa: set PORTA=8020 prima di lanciarlo da un terminale.
rem  Il server "di produzione" avviato da Windows (porta 8001) e' un'altra cosa e resta com'e'.
rem ============================================================================================
setlocal
cd /d "%~dp0"
title PANificator 360 - sviluppo

set "PMO=%~dp0.venv\Scripts\pmo.exe"
if not exist "%PMO%" (
    echo Ambiente .venv non trovato o progetto non installato.
    echo Dalla cartella del progetto esegui una volta:
    echo     py -3.12 -m venv .venv
    echo     .venv\Scripts\pip install -e ".[dev]"
    pause
    exit /b 1
)

if not defined PORTA set "PORTA=8010"
set "PMO_DATABASE_URL=sqlite:///data/prova.sqlite"
set "PMO_DEV_LOGIN=true"
set "PMO_SCHEDULER_ENABLED=false"
set "PMO_BASE_URL=http://localhost:%PORTA%"

netstat -ano | findstr /r /c:":%PORTA% .*LISTENING" >nul
if not errorlevel 1 (
    echo La porta %PORTA% e' gia' in uso: probabilmente il server e' gia' avviato in un'altra finestra.
    start "" "http://localhost:%PORTA%"
    pause
    exit /b 0
)

set "NUOVO="
if not exist "data\prova.sqlite" (
    if not exist "data" mkdir "data"
    if exist "data\pmo.sqlite" (
        echo Creo data\prova.sqlite come copia di data\pmo.sqlite...
        copy /y "data\pmo.sqlite" "data\prova.sqlite" >nul
    ) else (
        set "NUOVO=1"
    )
)

echo Aggiorno lo schema del database...
"%PMO%" init-db
if errorlevel 1 (
    echo Aggiornamento del database non riuscito: leggi l'errore qui sopra.
    pause
    exit /b 1
)
if defined NUOVO (
    echo Database nuovo: carico utenti, clienti e prodotti da config\seed.yaml...
    "%PMO%" seed
)

rem Il browser si apre dopo qualche secondo, quando il server e' pronto.
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start "" http://localhost:%PORTA%"

echo.
echo Server su http://localhost:%PORTA%  (Ctrl+C per fermarlo)
echo.
"%PMO%" serve --host 127.0.0.1 --port %PORTA% --reload

echo.
echo Server fermato.
pause
