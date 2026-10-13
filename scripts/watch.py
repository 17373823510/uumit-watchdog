#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UUMit 看门狗主程序
==================
一次运行 = 拉一次 UUMit 状态 → 与上次比对 → 有变化就唤醒 Agent。

用法：
    python watch.py --once                       # 单次检测（配合 cron 用）
    python watch.py --loop --max-seconds 19800   # 长驻循环（配合 GitHub Actions job 用）
    python watch.py --once --dry-run             # 只打印，不唤醒

环境变量：
    必需  UUMIT_API_KEY / UUMIT_USER_ID
    唤醒  WB_STORAGE_STATE_B64 或 WB_COOKIE，可选 WB_CHAT_URL
    通知  TG_BOT_TOKEN+TG_CHAT_ID / BARK_KEY / SERVERCHAN_KEY / NTFY_TOPIC
    行为  POLL_INTERVAL=60         轮询间隔（秒）
          WAKE_COOLDOWN=900        两次唤醒的最小间隔（秒）
          MAX_ITEMS=8              单条唤醒消息最多带几条任务
          DRY_RUN=1                只检测不唤醒
          GITHUB_REPOSITORY / GITHUB_REF_NAME   用于拼 raw 数据链接（Actions 自动注入）

退出码：
    0  正常结束
    2  凭据缺失或接口持续失败（配置类错误，需要人工处理）
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uumit_client import UumitClient, UumitError, brief_app, brief_order, classify  # noqa: E402
import notify                                                                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_STATE = os.path.join(ROOT, "state", "seen.json")
DEFAULT_PENDING = os.path.join(ROOT, "state", "pending.json")
CODEWORD = "AUTO"          # 与 /workspace/CLAUDE.md 中约定的暗号保持一致


def now():
    return int(time.time())


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print("[watch %s] %s" % (ts(), msg), flush=True)


# ---------------------------------------------------------------- 状态存取
def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("applications", {})
                d.setdefault("orders", {})
                d.setdefault("last_wakeup", 0)
                d.setdefault("last_notify", 0)
                return d
        except Exception as e:                           # noqa: BLE001
            log("状态文件损坏，重建：%s" % e)
    return {"applications": {}, "orders": {}, "last_wakeup": 0, "last_notify": 0}


def save_state(path, st):
    st["updated_at"] = ts()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_pending(path, events):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": ts(), "count": len(events), "items": events},
                  f, ensure_ascii=False, indent=2)
    log("已写入待办：%s（%d 项）" % (path, len(events)))


# ---------------------------------------------------------------- 差异计算
def diff(st, apps, orders):
    """
    返回 (events, first_run)
    event = {kind, id, title, raw_status, status, prev_status, task_id, ...}
    触发条件：
      - 申请：状态从 waiting 变成 accepted / unknown（unknown 视为「有变化，交给 Agent 判断」）
      - 申请：首次见到且状态已是 accepted（历史遗留也补报一次）
      - 订单：出现任何新 order_id
    """
    events = []
    first_run = not st["applications"] and not st["orders"]

    seen_apps = {}
    for a in apps:
        b = brief_app(a)
        if not b["app_id"]:
            continue
        seen_apps[b["app_id"]] = b["raw_status"] or ""
        prev = st["applications"].get(b["app_id"])
        cur = b["status"]
        if first_run:
            # 首次运行不补报历史，避免开箱即轰炸
            continue
        if prev is None:
            # 新出现的申请：只在已是「通过类」时报，pending 不报
            if cur in ("accepted", "unknown"):
                b["prev_status"] = None
                b["event"] = "new_application"
                events.append(b)
        else:
            prev_cls = classify(prev)
            if prev_cls != cur and cur in ("accepted", "unknown"):
                b["prev_status"] = prev
                b["event"] = "status_changed"
                events.append(b)

    seen_orders = {}
    for o in orders:
        b = brief_order(o)
        if not b["order_id"]:
            continue
        seen_orders[b["order_id"]] = b["raw_status"] or ""
        if first_run:
            continue
        if b["order_id"] not in st["orders"]:
            b["prev_status"] = None
            b["event"] = "new_order"
            events.append(b)

    st["applications"] = seen_apps
    st["orders"] = seen_orders
    return events, first_run


# ---------------------------------------------------------------- 消息
def raw_data_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        return ""
    branch = os.environ.get("GITHUB_REF_NAME", "").strip() or "main"
    return "https://raw.githubusercontent.com/%s/%s/state/pending.json" % (repo, branch)


def build_message(events):
    max_items = int(os.environ.get("MAX_ITEMS", "8"))
    lines = [CODEWORD, "【UUMit 新任务】%d 项" % len(events)]
    for i, e in enumerate(events[:max_items], 1):
        if e["kind"] == "application":
            lines.append("%d) [申请%s] %s | app=%s task=%s | %s→%s"
                         % (i, "通过" if e["status"] == "accepted" else "状态变化",
                            e["title"][:40], e["app_id"][:8], e["task_id"][:8],
                            e.get("prev_status") or "-", e["raw_status"]))
        else:
            lines.append("%d) [新订单] %s | order=%s task=%s | %s"
                         % (i, e["title"][:40], str(e["order_id"])[:8],
                            str(e.get("task_id"))[:8], e["raw_status"]))
    if len(events) > max_items:
        lines.append("... 另有 %d 项，见数据文件" % (len(events) - max_items))
    url = raw_data_url()
    if url:
        lines.append("数据：" + url)
    lines.append("按 AUTO 约定执行：扫描→派子代理写内容→上传→market-deliver 提交；回复压在 10 行内。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 一轮
def do_round(client, st, state_path, pending_path, dry=False):
    try:
        apps = client.my_applications()
        orders = client.seller_orders()
    except UumitError as e:
        log("❌ 拉取失败：%s" % e)
        return {"ok": False, "error": str(e)}

    events, first_run = diff(st, apps, orders)
    log("申请 %d / 订单 %d / 新事件 %d%s"
        % (len(apps), len(orders), len(events), "（首次运行，仅建基线）" if first_run else ""))

    result = {"ok": True, "events": events, "first_run": first_run, "wake": None}

    if not events:
        save_state(state_path, st)
        return result

    write_pending(pending_path, events)
    msg = build_message(events)
    log("唤醒消息预览：\n" + msg)

    if dry or os.environ.get("DRY_RUN") == "1":
        log("DRY_RUN=1，跳过唤醒")
        save_state(state_path, st)
        result["wake"] = "dry-run"
        return result

    cooldown = int(os.environ.get("WAKE_COOLDOWN", "900"))
    if now() - int(st.get("last_wakeup", 0)) < cooldown:
        left = cooldown - (now() - int(st.get("last_wakeup", 0)))
        log("唤醒节流中，%ds 后再试" % left)
        save_state(state_path, st)
        result["wake"] = "throttled"
        return result

    st["last_wakeup"] = now()
    save_state(state_path, st)

    cmd = [sys.executable, os.path.join(HERE, "wakeup.py"), "--message", msg]
    log("调用浏览器唤醒 ...")
    try:
        rc = subprocess.call(cmd, cwd=ROOT, timeout=300)
    except subprocess.TimeoutExpired:
        rc = -1
        log("唤醒脚本超时")

    if rc == 0:
        log("✅ 唤醒成功")
        result["wake"] = "sent"
        return result

    log("❌ 唤醒失败（rc=%s），走兜底通知" % rc)
    result["wake"] = "failed"
    if now() - int(st.get("last_notify", 0)) > 3600:
        st["last_notify"] = now()
        notify.send("UUMit 有新任务，但浏览器唤醒失败",
                    "手动发一句 “%s” 给 Agent 即可开工。\n\n%s" % (CODEWORD, msg),
                    important=True)
    save_state(state_path, st)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=int(os.environ.get("POLL_INTERVAL", "60")))
    ap.add_argument("--max-seconds", type=int, default=int(os.environ.get("MAX_SECONDS", "19800")))
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--pending", default=DEFAULT_PENDING)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.once and not a.loop:
        ap.print_help()
        return 0

    try:
        client = UumitClient()
    except UumitError as e:
        log("❌ %s" % e)
        return 2

    st = load_state(a.state)

    if a.once:
        r = do_round(client, st, a.state, a.pending, dry=a.dry_run)
        with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a", encoding="utf-8") as f:
            f.write("events=%d\n" % len(r.get("events", [])))
            f.write("wake=%s\n" % (r.get("wake") or "none"))
        return 0 if r.get("ok") else 2

    deadline = now() + a.max_seconds
    log("长驻循环启动：interval=%ss，最长 %ss" % (a.interval, a.max_seconds))
    consecutive_fail = 0
    while now() < deadline:
        try:
            r = do_round(client, st, a.state, a.pending, dry=a.dry_run)
            consecutive_fail = 0 if r.get("ok") else consecutive_fail + 1
        except Exception as e:                           # noqa: BLE001
            log("本轮异常：%s" % e)
            consecutive_fail += 1

        if consecutive_fail >= 5:
            log("连续 5 轮失败，退出（多半是凭据失效，请更新 Secrets）")
            notify.send("UUMit 看门狗已停止", "连续 5 轮拉取失败，请检查 UUMIT_API_KEY / UUMIT_USER_ID 是否失效。",
                        important=True)
            return 2

        # 距离 job 上限不足一轮就收尾，避免被强杀丢状态
        if now() + a.interval + 30 > deadline:
            log("接近 job 时长上限，正常收尾")
            break
        time.sleep(a.interval)

    save_state(a.state, st)
    log("循环结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
