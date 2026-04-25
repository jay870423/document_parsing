@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "CONFIG_FILE=%~1"
if "%CONFIG_FILE%"=="" set "CONFIG_FILE=.env.customer"

set "QDRANT_URL=http://127.0.0.1:6333"
for /f "tokens=1,* delims==" %%A in ('findstr /B /I "QDRANT_URL=" "%CONFIG_FILE%" 2^>nul') do set "QDRANT_URL=%%B"

echo [INFO] Qdrant target: %QDRANT_URL%

echo %QDRANT_URL% | findstr /I /C:"127.0.0.1" /C:"localhost" >nul
if errorlevel 1 (
  echo [INFO] QDRANT_URL is remote, skip local startup.
  exit /b 0
)

call :check_qdrant_health
if "!QDRANT_OK!"=="1" (
  echo [INFO] Qdrant is already running.
  exit /b 0
)

echo [INFO] Trying to start local Qdrant with Docker...
where docker >nul 2>nul
if not errorlevel 1 (
  if exist "docker-compose.yml" (
    docker compose up -d qdrant >nul 2>nul
    timeout /t 2 >nul
    call :check_qdrant_health
    if "!QDRANT_OK!"=="1" (
      echo [INFO] Qdrant started successfully.
      exit /b 0
    )
  )
)

echo [WARN] Local Qdrant is not running.
call :guide_install
call :check_qdrant_health
if "!QDRANT_OK!"=="1" (
  echo [INFO] Qdrant started successfully.
  exit /b 0
)

echo [ERROR] Qdrant is still unavailable.
echo [INFO] You can:
echo [INFO] 1) start Docker Desktop then rerun this script
echo [INFO] 2) switch QDRANT_URL in %CONFIG_FILE% to a remote Qdrant endpoint
exit /b 2

:check_qdrant_health
set "QDRANT_OK=0"
set "HEALTH_URL=%QDRANT_URL%/healthz"
curl.exe -fsS "%HEALTH_URL%" >nul 2>nul
if not errorlevel 1 set "QDRANT_OK=1"
exit /b 0

:guide_install
echo.
echo [GUIDE] Qdrant local startup guide
echo [GUIDE] Recommended: install Docker Desktop, then this script will auto-start qdrant.
echo.
where winget >nul 2>nul
if not errorlevel 1 (
  choice /C YN /N /M "Auto-install Docker Desktop now? [Y/N]: "
  if errorlevel 2 goto :manual_guide
  winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
)

:manual_guide
start "" "https://www.docker.com/products/docker-desktop/"
echo [GUIDE] 1) Install Docker Desktop
echo [GUIDE] 2) Launch Docker Desktop and wait until engine is running
echo [GUIDE] 3) Press any key, script will auto-start qdrant
pause >nul

where docker >nul 2>nul
if not errorlevel 1 (
  if exist "docker-compose.yml" (
    docker compose up -d qdrant >nul 2>nul
    timeout /t 2 >nul
  )
)
exit /b 0
