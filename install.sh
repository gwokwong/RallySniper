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
# dist 已随仓库提供（git 部署无需 node）；仅当 dist 缺失时才尝试本地构建
echo "[3/4] 前端产物检查 ..."
cd "$ROOT/frontend"
if [ -f dist/index.html ]; then
  echo "  ✓ dist 已随仓库提供，直接使用（无需 node 构建）"
else
  if ! command -v node >/dev/null 2>&1; then
    echo "❌ frontend/dist 缺失且本机无 node，无法提供 UI"
    echo "   修复：安装 node 后运行 npm ci && npm run build；或从本机拷贝 frontend/dist 目录"
    exit 1
  fi
  echo "  无 dist，使用 node $(node --version) 构建（需数分钟）..."
  npm ci --no-audit --no-fund || npm install --no-audit --no-fund
  npm run build || { echo "❌ 前端构建失败（可从本机拷贝 frontend/dist 后重试）"; exit 1; }
  [ -f dist/index.html ] || { echo "❌ 构建后仍无 dist/index.html"; exit 1; }
fi

# ---------- 3) 启动 ----------
echo "[4/4] 启动服务 ..."
bash "$ROOT/start.sh"

echo
echo "=============================================="
echo " ✅ 安装完成"
echo "=============================================="
