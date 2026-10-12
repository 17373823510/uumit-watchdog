#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交互式登录一次 WorkBuddy，导出 storage_state 的 base64
======================================================
导出的值填到 GitHub Secret `WB_STORAGE_STATE_B64`，
比裸 cookie 更耐用（带 localStorage，通常含刷新令牌）。

用法：
    python scripts/login_capture.py --phone 17373823510
    python scripts/login_capture.py --phone 173... --out /tmp/state.json

前置：
    pip install playwright && playwright install chromium
"""

import argparse
import base64
import getpass
import json
import os
import sys
import time

LOGIN_TPL = ("https://www.workbuddy.cn/login/?platform={platform}"
             "&state=0&redirect_uri=https%3A%2F%2Fwww.workbuddy.cn%2F{path}&product=workbuddy")

# 两个入口用的是 Keycloak 里不同的 client，登录态不互通，必须分别抓：
#   agents  -> /app   （目前唯一没有「停用新对话」的入口，优先用这个）
#   website -> /chat/ （网页端已停止发起新对话，仅作兜底）
PLATFORMS = {
    "agents":  {"path": "app",   "home": "https://www.workbuddy.cn/app"},
    "website": {"path": "",      "home": "https://www.workbuddy.cn/chat/"},
}


def log(m):
    print("[login] %s" % m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--platform", default="agents", choices=["agents", "website"],
                    help="抓哪个入口的登录态：agents=/app（推荐），website=/chat/")
    ap.add_argument("--out", default=None)
    ap.add_argument("--headless", default=os.environ.get("WB_HEADLESS", "1"))
    a = ap.parse_args()

    plat = PLATFORMS[a.platform]
    login_url = LOGIN_TPL.format(platform=a.platform, path=plat["path"])
    home = plat["home"]
    out = a.out or ("wb_storage_state_%s.json" % a.platform)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("请先安装：pip install playwright && playwright install chromium")
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=a.headless != "0",
                                     args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        log("打开登录页（platform=%s）..." % a.platform)
        page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)

        # 1) 协议同意弹窗（首次登录会出现，必须点掉，否则后续点击全部无效）
        for word in ["同意", "同意并继续", "我同意"]:
            try:
                btn = page.get_by_text(word, exact=False).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=3000)
                    log("已点击协议「%s」" % word)
                    page.wait_for_timeout(1500)
                    break
            except Exception:                            # noqa: BLE001
                pass

        # 2) 切到手机号登录
        for word in ["手机号登录", "手机号"]:
            try:
                tab = page.get_by_text(word, exact=False).first
                if tab.count() > 0 and tab.is_visible():
                    tab.click(timeout=3000)
                    log("已切换到「%s」" % word)
                    page.wait_for_timeout(1200)
                    break
            except Exception:                            # noqa: BLE001
                pass

        # 3) 填手机号
        filled = False
        for sel in ["#phoneNumber", "input[name='phoneNumber']",
                    "input[type='tel']", "input[placeholder*='手机号']"]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.fill(a.phone)
                    filled = True
                    log("已填入手机号")
                    break
            except Exception:                            # noqa: BLE001
                continue
        if not filled:
            log("❌ 找不到手机号输入框，请手动完成后把 storage_state 存出来")
            page.screenshot(path="login_manual.png")
            return 1

        # 4) 点「获取验证码」
        for word in ["获取验证码", "发送验证码", "获取动态码"]:
            try:
                btn = page.get_by_text(word, exact=False).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=4000)
                    log("已点击「%s」，短信应该马上到" % word)
                    break
            except Exception:                            # noqa: BLE001
                pass

        code = getpass.getpass("请输入短信验证码: ").strip()
        if not code:
            log("❌ 没收到验证码就退出了")
            return 1

        for sel in ["#code", "input[name='code']", "input[placeholder*='验证码']"]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.fill(code)
                    log("已填入验证码")
                    break
            except Exception:                            # noqa: BLE001
                continue

        for sel in ["#kc-login", "button[type='submit']"]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=4000)
                    break
            except Exception:                            # noqa: BLE001
                pass

        page.wait_for_timeout(6000)

        # 5) 验证登录结果
        log("回到目标入口验证：%s" % home)
        page.goto(home, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        cookies = ctx.cookies()
        names = {c["name"] for c in cookies}
        if "/login" in page.url or not ({"id", "session"} & names):
            log("❌ 登录似乎没成功（URL=%s，cookie=%d 个）" % (page.url, len(cookies)))
            page.screenshot(path="login_failed.png")
            return 1
        log("✅ 登录成功，cookie %d 个" % len(cookies))

        # 6) 顺手看看这个入口能不能发新对话
        body = ""
        try:
            body = page.inner_text("body")[:600]
        except Exception:                                # noqa: BLE001
            pass
        disabled = [m for m in ["已停止发起新对话", "已停用", "前往官网继续使用", "历史对话仅供查看"]
                    if m in body]
        has_input = False
        for f in page.frames:
            for sel in ["textarea", "div[contenteditable='true']", "[contenteditable='true']"]:
                try:
                    loc = f.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        has_input = True
                        break
                except Exception:                        # noqa: BLE001
                    continue
            if has_input:
                break
        if disabled:
            log("⚠️ 该入口提示：%s —— 发不了新对话，建议换另一个 platform 重抓" % "，".join(disabled))
        elif has_input:
            log("✅ 该入口有可用输入框，可以发新对话")
        else:
            log("⚠️ 未发现输入框，可能页面还没加载完，可用 --probe 再确认")

        ctx.storage_state(path=a.out)
        raw = open(a.out, "rb").read()
        b64 = base64.b64encode(raw).decode("ascii")
        log("已保存到 %s" % a.out)
        print("\n===== 下面这整段填到 GitHub Secret: WB_STORAGE_STATE_B64 =====\n")
        print(b64)
        print("\n===== 结束 =====\n")
        with open(a.out + ".b64", "w", encoding="utf-8") as f:
            f.write(b64)
        log("同时已写入 %s.b64" % a.out)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
