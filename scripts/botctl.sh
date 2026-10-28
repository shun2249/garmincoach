#!/usr/bin/env bash
# Telegram bot 的簡易控制（沒有裝 systemd 服務時用）。
#   scripts/botctl.sh start | stop | restart | status | log
# pid 與 log 放在 data/logs/。裝了 systemd 服務（deploy/garmin-coach-telegram.service）就不要再用這支，避免兩個 bot 同時跑。
set -e
cd "$(dirname "$0")/.."
mkdir -p data/logs
PID_FILE=data/logs/telegram_bot.pid
LOG=data/logs/telegram_bot.log
PY=.venv/bin/python

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }
others()  { pgrep -f "python -m garmin_coach telegram" | grep -v "^$(cat "$PID_FILE" 2>/dev/null || echo 0)$" || true; }

case "${1:-status}" in
  start)
    if running; then echo "已在執行 (pid $(cat "$PID_FILE"))"; exit 0; fi
    o=$(others); if [ -n "$o" ]; then echo "另有 bot 程序在跑：$o（先停掉，否則兩個會搶訊息）"; exit 1; fi
    nohup "$PY" -m garmin_coach telegram >> "$LOG" 2>&1 &
    echo $! > "$PID_FILE"; sleep 5
    if running; then echo "已啟動 (pid $(cat "$PID_FILE"))，log: $LOG"; tail -2 "$LOG"; else echo "啟動失敗，看 $LOG"; tail -20 "$LOG"; exit 1; fi ;;
  stop)
    if running; then
      kill "$(cat "$PID_FILE")"; for i in $(seq 1 20); do running || break; sleep 0.5; done
      running && { echo "沒停下來，強制結束"; kill -9 "$(cat "$PID_FILE")"; }
      rm -f "$PID_FILE"; echo "已停止"
    else echo "沒有在執行"; rm -f "$PID_FILE"; fi
    o=$(others); [ -n "$o" ] && echo "注意：還有其他 bot 程序：$o" || true ;;
  restart) "$0" stop; "$0" start ;;
  status)
    if running; then echo "執行中 (pid $(cat "$PID_FILE"))"; ps -o pid,etime,rss,cmd -p "$(cat "$PID_FILE")" | tail -1; else echo "未執行"; fi
    o=$(others); [ -n "$o" ] && echo "其他 bot 程序：$o" || true ;;
  log) tail -n "${2:-40}" -F "$LOG" ;;
  *) echo "用法: $0 start|stop|restart|status|log [行數]"; exit 1 ;;
esac
