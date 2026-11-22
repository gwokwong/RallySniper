#!/usr/bin/env bash
# ============================================================
# 盯盘系统一键启动（后台守护式，幂等：旧进程自动替换）
#   用法: ./start.sh          停止: ./stop.sh
#   后端: uvicorn 0.0.0.0:8000 —— /api + 直接托管 frontend/dist 页面
#   前端: vite preview 0.0.0.0:3006（本机有 node 且有 dist 时，可选）
#   日志: logs/backend.log  logs/frontend.log
# ============================================================
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR"

echo ""
echo "=========================================="
echo "  盯盘系统 启动中..."
echo "=========================================="

# 幂等：替换旧进程
bash "$ROOT_DIR/stop.sh" >/dev/null 2>&1 || true
sleep 1

# ---------- 后端 ----------
if [ ! -x "$BACKEND_DIR/.venv/bin/python" ]; then
  echo "❌ 未找到 backend/.venv —— 请先运行 ./install.sh"
  exit 1
fi
(cd "$BACKEND_DIR" && nohup .venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 > "$LOG_DIR/backend.log" 2>&1 &)

# ---------- 前端（可选：node + dist 才启动） ----------
if [ -x "$FRONTEND_DIR/node_modules/.bin/vite" ] && [ -d "$FRONTEND_DIR/dist" ]; then
  (cd "$FRONTEND_DIR" && nohup node_modules/.bin/vite preview \
    --host 0.0.0.0 --port 3006 > "$LOG_DIR/frontend.log" 2>&1 &)
fi

# ---------- 健康检查 ----------
printf "等待后端就绪 "
ok=""
for _ in $(seq 1 30); do
  if curl -s --noproxy '*' -f http://127.0.0.1:8000/api/health >/dev/null 2>&1; then ok=1; break; fi
  printf "."
  sleep 1
done
echo ""
if [ -z "$ok" ]; then
  echo "❌ 后端启动失败，最近日志："
  tail -20 "$LOG_DIR/backend.log"
  exit 1
fi

UI_OK=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ 2>/dev/null || echo 000)
echo "✅ 后端 API : http://127.0.0.1:8000/api/health"
if [ "$UI_OK" = "200" ]; then
  echo "✅ 系统页面 : http://127.0.0.1:8000/   （后端直接托管，部署推荐入口）"
fi
if [ -x "$FRONTEND_DIR/node_modules/.bin/vite" ] && [ -d "$FRONTEND_DIR/dist" ]; then
  sleep 2
  FE_OK=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:3006/ 2>/dev/null || echo 000)
  if [ "$FE_OK" = "200" ]; then
    echo "✅ 前端预览 : http://127.0.0.1:3006/   （/api 已代理到 8000）"
  else
    echo "⚠️ 前端预览未就绪，日志: logs/frontend.log"
  fi
fi
echo "日志目录   : $LOG_DIR"
echo "外部访问   : 把 127.0.0.1 换成服务器 IP；云服务器放行 8000/3006 端口"
echo ""
