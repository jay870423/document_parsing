@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "CONFIG_FILE=%~1"
if "%CONFIG_FILE%"=="" set "CONFIG_FILE=.env.customer"
echo [INFO] Using config file: %CONFIG_FILE%

if not exist "%CONFIG_FILE%" (
  if exist ".env.customer.example" (
    copy /Y ".env.customer.example" "%CONFIG_FILE%" >nul
    echo [INFO] Created %CONFIG_FILE% from .env.customer.example
  ) else (
    if exist ".env.example" (
      copy /Y ".env.example" "%CONFIG_FILE%" >nul
      echo [INFO] Created %CONFIG_FILE% from .env.example
    ) else (
      echo [ERROR] No env template found.
      pause
      exit /b 1
    )
  )
  echo [WARN] Please edit %CONFIG_FILE% and set ARK_API_KEY if needed.
)

set "PYTHON_BOOTSTRAP="
where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_BOOTSTRAP=py -3"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_BOOTSTRAP=python"
)

if "%PYTHON_BOOTSTRAP%"=="" (
  echo [ERROR] Python was not found.
  echo [INFO] Download: https://www.python.org/downloads/windows/
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating virtual environment...
  %PYTHON_BOOTSTRAP% -m venv .venv
  if errorlevel 1 goto :error
)

set "VENV_PY=.venv\Scripts\python.exe"

echo [INFO] Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :error
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :error

if exist "start_qdrant.bat" (
  call "start_qdrant.bat" "%CONFIG_FILE%"
  if errorlevel 2 (
    echo [WARN] Local Qdrant is not ready.
    choice /C YN /N /M "Continue starting app anyway? [Y/N]: "
    if errorlevel 2 exit /b 1
  )
)

set "APP_PORT=8000"
for /f "tokens=1,2 delims==" %%A in ('findstr /B /I "APP_PORT=" "%CONFIG_FILE%"') do set "APP_PORT=%%B"

set "APP_ENV_FILE=%CONFIG_FILE%"
echo [INFO] Starting service at http://127.0.0.1:%APP_PORT%
echo [INFO] Active config: %APP_ENV_FILE%

start "" "http://127.0.0.1:%APP_PORT%/"
"%VENV_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port %APP_PORT%
exit /b %ERRORLEVEL%

:error
echo [ERROR] Startup failed.
pause
exit /b 1
