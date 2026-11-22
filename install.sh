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
PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "❌ 未找到 python3，请先安装 Python 3.11+（推荐 3.13）"
  exit 1
fi
PYVER=$("$PY" -c "import sys; print('%d%02d' % (sys.version_info[0], sys.version_info[1]))")
if [ "$PYVER" -lt 311 ]; then
  echo "❌ Python 版本过低（$($PY --version)）：本系统依赖需要 3.11+，推荐 3.13"
  exit 1
fi
echo "[1/4] Python: $($PY --version)"

cd "$ROOT/backend"
# 虚拟环境不可跨机器移植（macOS 打包到 Linux 后 bin/python 是失效软链），
# 每次安装都全新创建，幂等且避免脏环境
if [ -d .venv ]; then
  echo "  移除旧 .venv（虚拟环境不可跨机器使用，将全新创建）..."
  rm -rf .venv
fi
echo "  创建虚拟环境 .venv ..."
"$PY" -m venv .venv 2>/tmp/gupiao_venv_err.log || true
if [ ! -x .venv/bin/python ]; then
  echo "❌ venv 创建失败（通常是缺少 ensurepip/venv 组件），错误信息："
  tail -5 /tmp/gupiao_venv_err.log 2>/dev/null
  echo "   修复命令："
  echo "     CentOS/OpenCloudOS:  dnf install -y python3.11 python3.11-devel"
  echo "     Ubuntu/Debian:       apt install -y python3.11-venv"
  echo "   修复后重新运行 ./install.sh"
  exit 1
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
