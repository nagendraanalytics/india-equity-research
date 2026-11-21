@echo off
rem ============================================================================
rem  ier.bat - every command for the India equity research system, in one file.
rem  Run from the project folder:   ier            (menu)
rem                                 ier api        (or any command below)
rem  Research tool for personal use. Not investment advice. No orders are placed.
rem ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"

rem --- secrets live in env.local.bat (never in this file, never in chat) -------
if not exist "env.local.bat" (
  > "env.local.bat" echo @echo off
  >> "env.local.bat" echo rem Your private settings. No quotes around values. Do not share this file.
  >> "env.local.bat" echo set IER_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/ier
  >> "env.local.bat" echo set IER_LLM_API_KEY=PASTE_YOUR_GROQ_KEY_HERE
  >> "env.local.bat" echo set IER_KITE_API_KEY=PASTE_YOUR_KITE_KEY_HERE
  >> "env.local.bat" echo set IER_KITE_API_SECRET=PASTE_YOUR_KITE_SECRET_HERE
  >> "env.local.bat" echo set IER_FRED_API_KEY=
  echo Created env.local.bat. Open it in Notepad, paste your keys, save, then run ier again.
  notepad "env.local.bat"
  exit /b 1
)
call "env.local.bat"

rem --- tools on PATH for this window --------------------------------------------
if exist "C:\pgsql\bin" set "PATH=C:\pgsql\bin;%PATH%"
if exist "C:\Program Files\nodejs" set "PATH=C:\Program Files\nodejs;%PATH%"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

set "CMD=%~1"
if "%CMD%"=="" goto menu
goto run

:menu
echo.
echo  India equity research
echo  ---------------------
echo   start          open API, watcher and dashboard in 3 windows
echo   api            API on http://127.0.0.1:8000
echo   watch          watcher: workers, file scan, source checks
echo   dashboard      dashboard on http://localhost:3000
echo   kite-login     daily Zerodha login ^(token ends 6 AM IST^)
echo   kite-prices    download daily prices into each company folder
echo   migrate        apply database changes ^(run after every update^)
echo   add-sector     load a classification CSV, seed and plan a sector
echo   plan           queue a run for a macro sector
echo   fix-universe   set listing date and liquidity so a sector passes the filter
echo   status         show job counts
echo   test           run the test suite ^(needs the test database^)
echo.
set /p CMD=Command:
if "%CMD%"=="" exit /b 0
set "ARG1="
set "ARG2="
if /i "%CMD%"=="add-sector" (
  set /p ARG1=CSV file name:
  set /p ARG2=Macro sector, for example Healthcare:
)
if /i "%CMD%"=="plan" set /p ARG1=Macro sector, for example Information Technology:
if /i "%CMD%"=="fix-universe" set /p ARG1=Macro sector, for example Healthcare:
goto run2

:run
set "ARG1=%~2"
set "ARG2=%~3"
:run2

if /i "%CMD%"=="start"        goto start
if /i "%CMD%"=="api"          goto api
if /i "%CMD%"=="watch"        goto watch
if /i "%CMD%"=="dashboard"    goto dashboard
if /i "%CMD%"=="kite-login"   goto kitelogin
if /i "%CMD%"=="kite-prices"  goto kiteprices
if /i "%CMD%"=="migrate"      goto migrate
if /i "%CMD%"=="add-sector"   goto addsector
if /i "%CMD%"=="plan"         goto plan
if /i "%CMD%"=="fix-universe" goto fixuniverse
if /i "%CMD%"=="status"       goto status
if /i "%CMD%"=="test"         goto test
echo Unknown command: %CMD%
exit /b 1

:start
start "IER api"       cmd /k "%~f0" api
start "IER watcher"   cmd /k "%~f0" watch
start "IER dashboard" cmd /k "%~f0" dashboard
echo Opened 3 windows. Dashboard: http://localhost:3000
exit /b 0

:api
python -m ier.api
exit /b %errorlevel%

:watch
python -m ier.runtime watch
exit /b %errorlevel%

:dashboard
cd dashboard
if not exist node_modules call npm install
call npm run dev
exit /b %errorlevel%

:kitelogin
python -m ier.runtime kite-login
exit /b %errorlevel%

:kiteprices
python -m ier.runtime kite-prices
exit /b %errorlevel%

:migrate
python -m alembic upgrade head
exit /b %errorlevel%

:addsector
if "%ARG1%"=="" echo Usage: ier add-sector file.csv "Macro Sector" & exit /b 1
if "%ARG2%"=="" echo Usage: ier add-sector file.csv "Macro Sector" & exit /b 1
python -m ier.runtime load-classification "%ARG1%" || exit /b 1
psql -U postgres -d ier -c "UPDATE instruments SET listing_date='2000-01-01', avg_daily_value=1000000000, status='active' WHERE macro_sector='%ARG2%' AND (listing_date IS NULL OR avg_daily_value IS NULL);"
python -m ier.runtime seed "%ARG2%" || exit /b 1
python -m ier.runtime plan "%ARG2%"
exit /b %errorlevel%

:plan
if "%ARG1%"=="" echo Usage: ier plan "Macro Sector" & exit /b 1
python -m ier.runtime plan "%ARG1%"
exit /b %errorlevel%

:fixuniverse
if "%ARG1%"=="" echo Usage: ier fix-universe "Macro Sector" & exit /b 1
psql -U postgres -d ier -c "UPDATE instruments SET listing_date='2000-01-01', avg_daily_value=1000000000, status='active' WHERE macro_sector='%ARG1%' AND (listing_date IS NULL OR avg_daily_value IS NULL);"
exit /b %errorlevel%

:status
psql -U postgres -d ier -c "SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status;"
exit /b %errorlevel%

:test
python -m pytest -q
exit /b %errorlevel%
