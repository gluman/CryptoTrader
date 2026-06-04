@echo off
REM ============================================
REM CryptoTrader - Setup Windows Services
REM Requires: NSSM (Non-Sucking Service Manager)
REM Download: https://nssm.cc/download
REM ============================================

set PROJECT_DIR=C:\Projects\СryptoTrader
set PYTHON_PATH=%PROJECT_DIR%\.venv\Scripts\python.exe
set NSSM_PATH=C:\Tools\nssm\nssm.exe

echo ============================================
echo CryptoTrader Service Installer
echo ============================================

REM Check NSSM
if not exist "%NSSM_PATH%" (
    echo ERROR: NSSM not found at %NSSM_PATH%
    echo Download from https://nssm.cc/download
    pause
    exit /b 1
)

REM Check Python venv
if not exist "%PYTHON_PATH%" (
    echo Creating virtual environment...
    python -m venv "%PROJECT_DIR%\.venv"
    "%PROJECT_DIR%\.venv\Scripts\pip.exe" install -r "%PROJECT_DIR%\requirements.txt"
)

echo.
echo Installing services...
echo.

REM ─── 1. API Server Service ───
echo [1/3] Installing CryptoTrader-API service...
"%NSSM_PATH%" install CryptoTrader-API "%PYTHON_PATH%" "%PROJECT_DIR%\main.py" --task api
"%NSSM_PATH%" set CryptoTrader-API AppDirectory "%PROJECT_DIR%"
"%NSSM_PATH%" set CryptoTrader-API DisplayName "CryptoTrader API Server"
"%NSSM_PATH%" set CryptoTrader-API Description "FastAPI server for CryptoTrader trading system"
"%NSSM_PATH%" set CryptoTrader-API Start SERVICE_AUTO_START
"%NSSM_PATH%" set CryptoTrader-API AppStdout "%PROJECT_DIR%\logs\api_stdout.log"
"%NSSM_PATH%" set CryptoTrader-API AppStderr "%PROJECT_DIR%\logs\api_stderr.log"
"%NSSM_PATH%" set CryptoTrader-API AppRotateFiles 1
"%NSSM_PATH%" set CryptoTrader-API AppRotateBytes 10485760

REM ─── 2. Scheduler Service ───
echo [2/3] Installing CryptoTrader-Scheduler service...
"%NSSM_PATH%" install CryptoTrader-Scheduler "%PYTHON_PATH%" "%PROJECT_DIR%\scheduler.py"
"%NSSM_PATH%" set CryptoTrader-Scheduler AppDirectory "%PROJECT_DIR%"
"%NSSM_PATH%" set CryptoTrader-Scheduler DisplayName "CryptoTrader Scheduler"
"%NSSM_PATH%" set CryptoTrader-Scheduler Description "Scheduled data collection, analysis, and execution"
"%NSSM_PATH%" set CryptoTrader-Scheduler Start SERVICE_AUTO_START
"%NSSM_PATH%" set CryptoTrader-Scheduler AppStdout "%PROJECT_DIR%\logs\scheduler_stdout.log"
"%NSSM_PATH%" set CryptoTrader-Scheduler AppStderr "%PROJECT_DIR%\logs\scheduler_stderr.log"
"%NSSM_PATH%" set CryptoTrader-Scheduler AppRotateFiles 1
"%NSSM_PATH%" set CryptoTrader-Scheduler AppRotateBytes 10485760

REM ─── 3. Health Monitor (optional) ───
echo [3/3] Installing CryptoTrader-Monitor service...
"%NSSM_PATH%" install CryptoTrader-Monitor "%PYTHON_PATH%" "%PROJECT_DIR%\scripts\health_monitor.py"
"%NSSM_PATH%" set CryptoTrader-Monitor AppDirectory "%PROJECT_DIR%"
"%NSSM_PATH%" set CryptoTrader-Monitor DisplayName "CryptoTrader Health Monitor"
"%NSSM_PATH%" set CryptoTrader-Monitor Description "Monitors system health and restarts failed services"
"%NSSM_PATH%" set CryptoTrader-Monitor Start SERVICE_AUTO_START
"%NSSM_PATH%" set CryptoTrader-Monitor DependOnService CryptoTrader-API CryptoTrader-Scheduler

echo.
echo ============================================
echo Services installed successfully!
echo ============================================
echo.
echo To start services:
echo   nssm start CryptoTrader-API
echo   nssm start CryptoTrader-Scheduler
echo   nssm start CryptoTrader-Monitor
echo.
echo To view logs:
echo   type logs\api_stdout.log
echo   type logs\scheduler_stdout.log
echo.
pause
