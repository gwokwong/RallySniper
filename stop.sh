#!/usr/bin/env bash
# 停止盯盘系统（后端 8000 / 前端 3006）
if pkill -f "uvicorn app.main:app" 2>/dev/null; then
  echo "✅ 后端已停止"
else
  echo "ℹ️ 后端未在运行"
fi
if pkill -f "vite preview" 2>/dev/null; then
  echo "✅ 前端预览已停止"
else
  echo "ℹ️ 前端预览未在运行"
fi
