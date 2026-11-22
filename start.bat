@echo off
chcp 65001 >nul
REM ============================================================
REM 盯盘系统一键启动脚本 (Windows)
REM 用法: 双击 start.bat
REM ============================================================
setlocal

set "ROOT_DIR=%~dp0"
set "BACKEND_DIR=%ROOT_DIR%backend"
set "FRONTEND_DIR=%ROOT_DIR%frontend"
set "VENV_DIR=%BACKEND_DIR%\.venv"

echo.
echo ==========================================
echo   盯盘系统 启动中...
echo ==========================================
echo.

REM ---------- 1. 后端虚拟环境 ----------
if not exist "%VENV_DIR%" (
  echo [1/4] 创建 Python 虚拟环境...
  python -m venv "%VENV_DIR%"
) else (
  echo [1/4] Python 虚拟环境已存在
)

REM ---------- 2. 后端依赖 ----------
echo [2/4] 检查后端依赖...
"%VENV_DIR%\Scripts\python.exe" -c "import fastapi, akshare, uvicorn" 2>nul
if errorlevel 1 (
  echo       正在安装后端依赖（首次运行较慢，请耐心等待）...
  "%VENV_DIR%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV_DIR%\Scripts\python.exe" -m pip install --quiet fastapi "uvicorn[standard]" akshare pandas pydantic-settings httpx apscheduler
) else (
  echo       后端依赖已就绪
)

REM ---------- 3. 前端依赖 ----------
echo [3/4] 检查前端依赖...
if not exist "%FRONTEND_DIR%\node_modules\.bin\vite.cmd" (
  echo       正在安装前端依赖...
  pushd "%FRONTEND_DIR%"
  call npm install --no-audit --no-fund
  popd
) else (
  echo       前端依赖已就绪
)

REM ---------- 4. 启动服务 ----------
echo [4/4] 启动服务...
echo.

start "StockMonitor-Backend" /D "%BACKEND_DIR%" cmd /c ""%VENV_DIR%\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
timeout /t 5 /nobreak >nul
start "StockMonitor-Frontend" /D "%FRONTEND_DIR%" cmd /c "npm run dev -- --host 0.0.0.0"

echo.
echo ==========================================
echo   启动完成！
echo ------------------------------------------
echo   前端界面:  http://localhost:3006
echo   后端 API:  http://localhost:8000/api/health
echo ------------------------------------------
echo   关闭时请分别关闭弹出的两个服务窗口
echo ==========================================
echo.
pause
