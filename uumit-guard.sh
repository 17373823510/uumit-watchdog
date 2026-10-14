#!/usr/bin/env bash
# 看门狗自杀守护：每分钟探活 uumit-poll.sh，进程不在就拉起。
# 背景常驻；用 .guard.lock 防重复；本机沙箱无 crontab，故用 nohup 常驻。
set -u
RT="/root/.codebuddy/skills/uumit-agent/memory/runtime"
mkdir -p "$RT"
LOCK="$RT/.guard.lock"
LOG="$RT/guard.log"
PROBE="bash /workspace/uumit-poll.sh"

if [ -f "$LOCK" ]; then
  OPID="$(cat "$LOCK" 2>/dev/null || echo)"
  if [ -n "$OPID" ] && kill -0 "$OPID" 2>/dev/null; then exit 0; fi
  rm -f "$LOCK"
fi
echo $$ > "$LOCK"
trap '[ "$(cat "$LOCK" 2>/dev/null)" = "$$" ] && rm -f "$LOCK"' EXIT

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] guard start pid=$$" >> "$LOG"
while true; do
  if ! pgrep -f "uumit-poll.sh" >/dev/null 2>&1; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] poll 进程缺失，拉起" >> "$LOG"
    nohup $PROBE >/tmp/uumit-poll-boot.log 2>&1 &
  fi
  sleep 60
done
