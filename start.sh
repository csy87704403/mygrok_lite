#!/bin/bash
# Grok 账号管理平台 - 启动脚本
# 用法: ./start.sh [start|stop|restart|status]

PLATFORM_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$PLATFORM_DIR/data/platform.pid"
LOG_FILE="/tmp/grok_platform.log"

start() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
    echo "平台已在运行 (PID $(cat $PID_FILE))"
    return
  fi
  echo "启动平台..."
  cd "$PLATFORM_DIR" || exit 1
  nohup /usr/bin/python3.11 main.py >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 2
  # 健康检查
  if curl -s http://127.0.0.1:18080/health > /dev/null 2>&1; then
    echo "✅ 平台启动成功 (PID $(cat $PID_FILE), 端口 18080)"
  else
    echo "⚠️ 平台启动可能失败, 查看日志: $LOG_FILE"
  fi
}

stop() {
  if [ -f "$PID_FILE" ]; then
    kill "$(cat $PID_FILE)" 2>/dev/null
    rm -f "$PID_FILE"
    echo "平台已停止"
  else
    echo "平台未运行"
  fi
}

status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
    echo "✅ 平台运行中 (PID $(cat $PID_FILE))"
    curl -s http://127.0.0.1:18080/health
  else
    echo "❌ 平台未运行"
  fi
}

case "$1" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  *) echo "用法: $0 {start|stop|restart|status}"; exit 1 ;;
esac
