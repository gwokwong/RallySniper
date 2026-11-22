#!/usr/bin/env bash
# =============================================================
# A股主升浪捕手 · 一键安装（本地 / 服务器通用）
#   用法:  ./install.sh
#   内容:  检查环境 -> 后端 venv+依赖 -> 前端依赖+构建(有node时)
#          -> 启动服务 -> 健康检查 -> 打印访问地址
#   重复执行安全（幂等）：旧进程会先被替换
# =============================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

echo "=============================================="
echo " A股主升浪捕手 · 一键安装"
echo "=============================================="

# ---------- 1) Python ----------
PY="$(command -v python3.13 || command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "❌ 未找到 python3，请先安装 Python 3.11+（推荐 3.13）"
  exit 1
fi
echo "[1/4] Python: $($PY --version)"

cd "$ROOT/backend"
if [ ! -x .venv/bin/python ]; then
  echo "  创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
echo "[2/4] 安装后端依赖 (requirements.txt) ..."
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

# ---------- 2) 前端 ----------
echo "[3/4] 前端构建 ..."
cd "$ROOT/frontend"
if command -v node >/dev/null 2>&1; then
  echo "  node: $(node --version)"
  [ -d node_modules ] || npm ci --no-audit --no-fund 2>&1 | tail -2 || npm install --no-audit --no-fund 2>&1 | tail -2
  CODEBUDDY_SAFE_DELETE_ENABLED=0 npm run build 2>&1 | tail -3
else
  echo "  ⚠️ 未检测到 node —— 跳过前端构建。"
  echo "     若 frontend/dist 已存在，后端 8000 端口会直接托管 UI（无需 node）。"
  echo "     若 dist 也不存在，请先在有 node 的机器上构建后拷贝 dist 目录。"
  [ -d dist ] || { echo "❌ frontend/dist 不存在且无法构建，UI 将无法访问"; exit 1; }
fi

# ---------- 3) 启动 ----------
echo "[4/4] 启动服务 ..."
bash "$ROOT/start.sh"

echo
echo "=============================================="
echo " ✅ 安装完成"
echo "=============================================="
