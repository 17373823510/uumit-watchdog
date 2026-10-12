#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UUMit 只读客户端
================
零第三方依赖（只用标准库 urllib），可在任何 Python 3.8+ 环境运行，
专为 GitHub Actions runner 设计。

认证方式与官方 skill 一致：
    X-Api-Key:          <api_key>
    X-Platform-User-Id: <platform_user_id>

所有凭据一律从环境变量读取，禁止硬编码。
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://api.uumit.com"
DEFAULT_TIMEOUT = 25

# ---------------------------------------------------------------- 状态分类
# 申请状态没有官方枚举文档，实测只见过 "pending"。
# 这里做「宽容识别」：认识的词按语义归类，不认识的词一律当作 unknown 并上报，
# 由 Agent 侧判断，避免漏掉真正通过的申请。
POSITIVE = {
    "accepted", "approved", "hired", "selected", "agreed",
    "won", "confirmed", "matched", "success", "passed",
}
NEGATIVE = {
    "rejected", "declined", "withdrawn", "canceled", "cancelled",
    "expired", "closed", "refused", "failed", "invalid",
}
WAITING = {
    "pending", "applied", "reviewing", "submitted", "waiting",
    "under_review", "in_review",
}


def classify(status):
    """把原始 status 归一到 accepted / rejected / waiting / unknown。"""
    s = (status or "").strip().lower()
    if s in POSITIVE:
        return "accepted"
    if s in NEGATIVE:
        return "rejected"
    if s in WAITING:
        return "waiting"
    return "unknown"


class UumitError(Exception):
    pass


class UumitClient:
    def __init__(self, api_key=None, user_id=None, base=None, timeout=DEFAULT_TIMEOUT):
        self.api_key = (api_key or os.environ.get("UUMIT_API_KEY", "")).strip()
        self.user_id = (user_id or os.environ.get("UUMIT_USER_ID", "")).strip()
        self.base = (base or os.environ.get("UUMIT_BASE_URL", DEFAULT_BASE)).rstrip("/")
        self.timeout = timeout
        if not self.api_key or not self.user_id:
            raise UumitError("缺少凭据：需设置 UUMIT_API_KEY 与 UUMIT_USER_ID")

    # ------------------------------------------------------------ 底层请求
    def get(self, path, params=None, retries=3):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/json",
            "User-Agent": "uumit-watchdog/1.0 (+github-actions)",
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if self.user_id:
            headers["X-Platform-User-Id"] = self.user_id

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw.strip() else {}
                if isinstance(data, dict) and data.get("code") not in (0, "0", None):
                    raise UumitError("接口返回错误: code=%s message=%s"
                                     % (data.get("code"), data.get("message")))
                return data
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                last_err = "HTTP %s %s" % (e.code, body)
                # 401/403 是凭据问题，重试没意义
                if e.code in (401, 403):
                    raise UumitError("凭据无效或被拒绝: " + last_err)
            except Exception as e:                       # noqa: BLE001
                last_err = "%s: %s" % (type(e).__name__, e)

            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))
        raise UumitError("请求失败 %s -> %s" % (path, last_err))

    # ------------------------------------------------------------ 业务封装
    def my_applications(self, page_size=100):
        """我的接单申请列表。"""
        data = self.get("/api/v1/tasks/applications/mine",
                        {"page": 1, "page_size": page_size})
        d = data.get("data") or {}
        items = d.get("items") if isinstance(d, dict) else (d or [])
        return items or []

    def seller_orders(self, page_size=100):
        """作为接单方（卖家）的订单列表。"""
        data = self.get("/api/v1/orders", {"role": "seller", "page": 1,
                                           "page_size": page_size})
        d = data.get("data") or {}
        items = d.get("items") if isinstance(d, dict) else (d or [])
        return items or []

    def task_detail(self, task_id):
        """任务详情（拿标题/预算/交付要求），失败时返回 None，不阻断主流程。"""
        try:
            data = self.get("/api/v1/tasks/%s" % task_id)
            return (data.get("data") or {}) or None
        except Exception:                                # noqa: BLE001
            return None

    def seller_pending_count(self):
        try:
            data = self.get("/api/v1/orders/seller-pending-count")
            return (data.get("data") or {})
        except Exception:                                # noqa: BLE001
            return {}


# ---------------------------------------------------------------- 结构提取
def brief_app(a):
    """从申请记录里抽出关心的字段。"""
    return {
        "kind": "application",
        "app_id": a.get("id"),
        "task_id": a.get("task_id"),
        "title": a.get("task_title") or a.get("title") or "(无标题)",
        "raw_status": a.get("status"),
        "status": classify(a.get("status")),
        "responded_at": a.get("responded_at"),
        "applied_at": a.get("applied_at"),
    }


def brief_order(o):
    return {
        "kind": "order",
        "order_id": o.get("id") or o.get("order_id"),
        "task_id": o.get("task_id"),
        "title": o.get("title") or o.get("task_title") or "(无标题)",
        "raw_status": o.get("status"),
        "status": classify(o.get("status")) if o.get("status") else "unknown",
        "bounty": o.get("bounty_amount") or o.get("amount"),
        "created_at": o.get("created_at") or o.get("ordered_at"),
    }


if __name__ == "__main__":
    # 自检模式：python uumit_client.py
    try:
        c = UumitClient()
        apps = c.my_applications()
        orders = c.seller_orders()
        print("申请 %d 条，订单 %d 条" % (len(apps), len(orders)))
        from collections import Counter
        print("申请状态分布:", dict(Counter(a.get("status") for a in apps)))
        for a in apps[:5]:
            print("  -", brief_app(a))
        for o in orders[:5]:
            print("  *", brief_order(o))
    except UumitError as e:
        print("FAIL:", e)
        raise SystemExit(1)
