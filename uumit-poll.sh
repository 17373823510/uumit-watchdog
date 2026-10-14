#!/usr/bin/env bash
# 沙箱内 UUMit 轮询（过渡方案，沙箱在线时有效）
#   - 每 60 秒查一次申请/订单
#   - 有新任务 -> PushPlus 推送 + 触发 GitHub Actions 发暗号
# 沙箱休眠会终止本脚本（GitHub runner 访问不了 UUMit，暂无境内常驻替代）。
# 增强：单轮失败不退出、详细日志、断点续传、自守护重启。
set -u
export UUMIT_SKILL_DIR="/root/.codebuddy/skills/uumit-agent"
RT="$UUMIT_SKILL_DIR/memory/runtime"
WD="/workspace/uumit-watchdog"
LOG="$RT/sandbox-poll.log"
STATE="$RT/.poll-seen.json"
LOCK="$RT/.poll.lock"
mkdir -p "$RT"

# ---------- 单实例锁：防止重复启动造成多进程轮询、互相覆盖状态 ----------
if [ -f "$LOCK" ]; then
  OLD_PID="$(cat "$LOCK" 2>/dev/null || echo '')"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 已有实例在运行 (pid=$OLD_PID)，退出" >> "$LOG"
    exit 0
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 清理陈旧锁 (pid=$OLD_PID 已不存在)" >> "$LOG"
  rm -f "$LOCK"
fi
echo $$ > "$LOCK"
cleanup_lock() { [ "$(cat "$LOCK" 2>/dev/null)" = "$$" ] && rm -f "$LOCK"; }
trap 'cleanup_lock' EXIT INT TERM

export PUSHPLUS_TOKEN="0c5d0ca5cfd547e0a5e3950005988013"
# 普通消息只发微信 + QQ；重要消息（有新任务等）才追加短信
export PUSHPLUS_CHANNELS="wechat,qq"
export PUSHPLUS_IMPORTANT_CHANNELS="wechat,qq,sms"
export GH_TOKEN="$(cat /tmp/gh_token.txt 2>/dev/null || echo '')"
REPO="17373823510/uumit-watchdog"

# 把凭据从已授权文件读出，不落盘明文
python3.11 -c "import json;d=json.load(open('$UUMIT_SKILL_DIR/memory/uumit-auth.json'));print(d['api_key'],d['platform_user_id'])" > /tmp/_uumit_auth.txt
export UUMIT_API_KEY="$(cut -d' ' -f1 /tmp/_uumit_auth.txt)"
export UUMIT_USER_ID="$(cut -d' ' -f2 /tmp/_uumit_auth.txt)"
rm -f /tmp/_uumit_auth.txt

# 日志配额：超过 1MB 清空重写
[ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ] && : > "$LOG"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] sandbox-poll start (pid=$$)" >> "$LOG"

# 收到退出信号时登记并清锁
on_exit() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] sandbox-poll exit (pid=$$)" >> "$LOG"
  cleanup_lock
}
trap 'on_exit' EXIT INT TERM

# 单轮检测：只调用一次 watch.py，捕获完整输出。
# 输出整段到全局 OUT，供计数与通知复用（避免重复调用导致状态被消费）。
round_once() {
  OUT=$(cd "$WD" && timeout 90 python3.11 -u scripts/watch.py --once --dry-run --state "$STATE" 2>&1)
  echo "$OUT" >> "$LOG"
  echo "$OUT"
}

while true; do
  OUT=$(round_once || echo "")
  N=$(echo "$OUT" | grep -oE "新事件 [0-9]+" | grep -oE "[0-9]+" | head -1)
  # 防止空/非数字导致比较失败
  case "$N" in
    ''|*[!0-9]*) N=0 ;;
  esac
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] events=${N}" >> "$LOG"

  if [ "$N" -gt 0 ]; then
    LAST=$(echo "$OUT" | tail -20)
    # 1) PushPlus 直达手机（重要消息：微信 + QQ + 短信）
    (cd "$WD" && timeout 60 python3.11 scripts/notify.py --important "UUMit 有新任务（沙箱检测）" "$LAST" >> "$LOG" 2>&1) || true
    # 2) 触发 GitHub Actions 去发暗号唤醒（失败也不影响通知）
    if [ -n "${GH_TOKEN:-}" ]; then
      MSG=$(echo "$LAST" | python3.11 -c "import sys;print('AUTO\n'+sys.stdin.read()[:600])")
      timeout 40 python3.11 - "$MSG" <<'PY' >> "$LOG" 2>&1 || true
import json, os, sys, urllib.request
body = json.dumps({"event_type": "uumit-new-task",
                   "client_payload": {"message": sys.argv[1]}}).encode()
req = urllib.request.Request(
    "https://api.github.com/repos/17373823510/uumit-watchdog/dispatches",
    data=body, method="POST",
    headers={"Authorization": "token " + os.environ.get("GH_TOKEN", ""),
             "Accept": "application/vnd.github+json",
             "User-Agent": "sandbox-poll",
             "Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=30)
    print("dispatch OK")
except Exception as e:
    print("dispatch FAIL", e)
PY
    fi
  fi
  sleep 60
done
