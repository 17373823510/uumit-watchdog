#!/usr/bin/env bash
# UUMit 看门狗 幂等自愈启动器
# 用途：沙箱休眠会杀掉所有后台进程（nohup/setsid 都无法跨休眠存活）。
# 本脚本可安全重复执行：已在跑就什么都不做，没在跑就拉起 poll + guard。
# 用法：bash /workspace/uumit-ensure.sh        （静默自愈）
#       bash /workspace/uumit-ensure.sh status  （只看状态）
set -u
RT="/root/.codebuddy/skills/uumit-agent/memory/runtime"
SKILL_DIR="/root/.codebuddy/skills/uumit-agent"
mkdir -p "$RT"

running() { pgrep -f "uumit-poll.sh" >/dev/null 2>&1; }
guard_running() { pgrep -f "uumit-guard.sh" >/dev/null 2>&1; }

if [ "${1:-}" = "status" ]; then
  echo "poll:  $(running && echo 运行中 || echo 已停止)"
  echo "guard: $(guard_running && echo 运行中 || echo 已停止)"
  echo "最后轮询: $(tail -1 "$RT/sandbox-poll.log" 2>/dev/null)"
  exit 0
fi

# 清陈旧锁
[ -f "$RT/.poll.lock" ] && { OP=$(cat "$RT/.poll.lock" 2>/dev/null); kill -0 "$OP" 2>/dev/null || rm -f "$RT/.poll.lock"; }
[ -f "$RT/.guard.lock" ] && { OG=$(cat "$RT/.guard.lock" 2>/dev/null); kill -0 "$OG" 2>/dev/null || rm -f "$RT/.guard.lock"; }

export UUMIT_SKILL_DIR="$SKILL_DIR"
export PUSHPLUS_TOKEN="0c5d0ca5cfd547e0a5e3950005988013"
export UUMIT_API_KEY="$(python3.11 -c "import json;print(json.load(open('$SKILL_DIR/memory/uumit-auth.json'))['api_key'])")"
export UUMIT_USER_ID="$(python3.11 -c "import json;print(json.load(open('$SKILL_DIR/memory/uumit-auth.json'))['platform_user_id'])")"

if running; then
  echo "[ensure] poll 已在运行，跳过"
else
  setsid bash /workspace/uumit-poll.sh >/tmp/uumit-poll-boot.log 2>&1 </dev/null &
  echo "[ensure] poll 已拉起"
fi

if guard_running; then
  echo "[ensure] guard 已在运行，跳过"
else
  setsid bash /workspace/uumit-guard.sh >/tmp/uumit-guard-boot.log 2>&1 </dev/null &
  echo "[ensure] guard 已拉起"
fi
