#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 网页端「浏览器唤醒」
==============================
用 Playwright 打开 WorkBuddy 网页版，在会话输入框里发出约定的暗号消息，
从而把 Agent（我）唤醒去处理 UUMit 任务。

用法：
    python wakeup.py --message "AUTO ..."        # 发送指定消息
    python wakeup.py --check                     # 只检查登录态
    python wakeup.py --probe                     # 探测哪个入口可发消息（调试用）

凭据（全部走环境变量，禁止落盘/硬编码）：
    WB_STORAGE_STATE_B64   Playwright storage_state.json 的 base64（推荐，含 localStorage，最耐用）
    WB_COOKIE              Cookie 字符串，形如 "a=1; b=2"（次选）
    WB_CHAT_URL            可选。直接指定会话页 URL，命中则优先使用

环境变量（可选）：
    WB_HEADLESS            默认 1；设 0 可在本地看画面
    WAKE_SHOT_DIR          截图输出目录，默认 ./shots

退出码：0 成功 / 1 失败
"""

import argparse
import base64
import json
import os
import sys
import time

HOME = "https://www.workbuddy.cn/"

# 登录入口（用于判断是否掉登录）
LOGIN_MARKERS = ["/login", "扫码登录", "手机号登录", "kc-login"]

# 网页端如果关闭了「发起新对话」，页面上会出现这些文案
DISABLED_MARKERS = [
    "已停止发起新对话", "已停用", "停止发起新对话",
    "网页端已停止", "前往官网继续使用", "历史对话仅供查看",
]

INPUT_SELECTORS = [
    "textarea",
    "div[contenteditable='true']",
    "[contenteditable='true']",
    "input[type='text'][placeholder*='输入']",
    "#chat-input",
    "[data-testid*='input']",
    "[class*='chat-input'] textarea",
    "[class*='ChatInput'] textarea",
    "[class*='input'] textarea",
]

SEND_BTN_SELECTORS = [
    "button[aria-label*='发送']",
    "button[aria-label*='Send']",
    "button[type='submit']",
    "[class*='send-btn']",
    "[class*='sendBtn']",
    "[class*='SendButton']",
    "button:has-text('发送')",
]

SHOT_DIR = os.environ.get("WAKE_SHOT_DIR", "shots")
HEADLESS = os.environ.get("WB_HEADLESS", "1") != "0"


def log(msg):
    print("[wakeup %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 凭据
def load_state_path():
    """把 base64 的 storage_state 还原成临时文件，返回路径或 None。"""
    b64 = (os.environ.get("WB_STORAGE_STATE_B64", "") or "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        obj = json.loads(raw.decode("utf-8"))
        assert "cookies" in obj
    except Exception as e:                               # noqa: BLE001
        log("WB_STORAGE_STATE_B64 解析失败：%s" % e)
        return None
    p = os.path.join(SHOT_DIR, "_storage_state.json")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw.decode("utf-8"))
    log("已从环境变量还原 storage_state（%d 个 cookie）" % len(obj.get("cookies", [])))
    return p


def parse_cookie_string(raw, domain=".workbuddy.cn"):
    if raw.strip().startswith("["):
        try:
            return json.loads(raw)
        except Exception:                                # noqa: BLE001
            return []
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        out.append({"name": name.strip(), "value": value.strip(),
                    "domain": domain, "path": "/"})
    return out


def build_context(pw, browser):
    """按可用凭据创建浏览器上下文。返回 (ctx, mode) 或 (None, None)。"""
    state_path = load_state_path()
    if state_path:
        try:
            ctx = browser.new_context(storage_state=state_path,
                                      viewport={"width": 1440, "height": 900})
            return ctx, "storage_state"
        except Exception as e:                           # noqa: BLE001
            log("storage_state 装载失败：%s" % e)

    raw = (os.environ.get("WB_COOKIE", "") or "").strip()
    if raw:
        cookies = parse_cookie_string(raw)
        if not cookies:
            log("WB_COOKIE 解析为空")
            return None, None
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        added = 0
        for c in cookies:
            try:
                ctx.add_cookies([c])
                added += 1
            except Exception:                            # noqa: BLE001
                pass
        log("注入 cookie %d/%d 个" % (added, len(cookies)))
        return ctx, "cookie"

    log("❌ 没有任何凭据：请设置 WB_STORAGE_STATE_B64 或 WB_COOKIE")
    return None, None


# ---------------------------------------------------------------- 页面判断
def page_text(page, limit=1200):
    try:
        return page.inner_text("body")[:limit]
    except Exception:                                    # noqa: BLE001
        return ""


def looks_logged_out(page, text):
    if "/login" in (page.url or ""):
        return True
    return any(m in text for m in LOGIN_MARKERS)


def looks_disabled(text):
    return any(m in text for m in DISABLED_MARKERS)


def find_input(page):
    for frame in page.frames:
        for sel in INPUT_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return frame, loc, sel
            except Exception:                            # noqa: BLE001
                continue
    return None


def candidate_urls():
    urls = []
    custom = (os.environ.get("WB_CHAT_URL", "") or "").strip()
    if custom:
        urls.append(custom)
    for u in ["https://www.workbuddy.cn/app",
              "https://www.workbuddy.cn/chat/",
              "https://www.workbuddy.cn/chat",
              HOME]:
        if u not in urls:
            urls.append(u)
    return urls


def try_send(page, text):
    got = find_input(page)
    if not got:
        return False, "未找到输入框"
    frame, loc, sel = got
    log("找到输入框：%s" % sel)

    try:
        loc.click(timeout=6000)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        loc.fill(text)
        log("已填入 %d 字" % len(text))
    except Exception:                                    # noqa: BLE001
        try:
            frame.evaluate(
                """(t) => {
                    const el = document.querySelector(
                      "textarea, div[contenteditable='true'], [contenteditable='true']");
                    if (!el) return false;
                    el.focus();
                    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') el.value = t;
                    else el.innerText = t;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    return true;
                }""", text)
            log("已通过 JS 注入文本")
        except Exception as e:                           # noqa: BLE001
            return False, "填文本失败：%s" % e

    time.sleep(1.0)

    for f in page.frames:
        for bsel in SEND_BTN_SELECTORS:
            try:
                btn = f.locator(bsel).first
                if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                    btn.click(timeout=4000)
                    log("点击发送按钮：%s" % bsel)
                    return True, "button"
            except Exception:                            # noqa: BLE001
                continue

    try:
        loc.press("Enter")
        log("按 Enter 发送")
        return True, "enter"
    except Exception as e:                               # noqa: BLE001
        return False, "Enter 失败：%s" % e


def verify(page, text, timeout=25):
    """回读页面，确认消息真的发出去了。"""
    probe = text.strip().splitlines()[0][:16]
    if not probe:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        if probe in page_text(page, limit=8000):
            return True
    return False


def shot(page, name):
    os.makedirs(SHOT_DIR, exist_ok=True)
    p = os.path.join(SHOT_DIR, name)
    try:
        page.screenshot(path=p, full_page=False)
        log("截图：%s" % p)
    except Exception as e:                               # noqa: BLE001
        log("截图失败：%s" % e)
    return p


# ---------------------------------------------------------------- 主流程
def run(message=None, mode="send"):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("❌ 未安装 playwright，请先 pip install playwright && playwright install chromium")
        return 1

    os.makedirs(SHOT_DIR, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        ctx, cmode = build_context(pw, browser)
        if ctx is None:
            browser.close()
            return 1
        log("凭据模式：%s" % cmode)

        page = ctx.new_page()
        results = []
        sent = False

        for url in candidate_urls():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:                       # noqa: BLE001
                log("%s -> 打开失败 %s" % (url, str(e)[:70]))
                results.append((url, "open-failed"))
                continue
            page.wait_for_timeout(4500)
            text = page_text(page)
            log("%s -> 落地 %s" % (url, page.url[:90]))

            if looks_logged_out(page, text):
                # 注意：不同入口可能是不同的 OIDC client（/app 是 platform=agents，
                # /chat/ 是 platform=website），一个入口跳登录不代表凭据整体失效，
                # 必须继续试下一个入口，全部跳登录才判定凭据失效。
                log("  ❌ 该入口未登录，换下一个")
                results.append((url, "logged-out"))
                shot(page, "logged_out_%d.png" % len(results))
                continue

            if looks_disabled(text):
                log("  ⚠️ 该入口已停用发起新对话")
                results.append((url, "disabled"))
                continue

            if mode == "check":
                log("  ✅ 已登录")
                shot(page, "check.png")
                browser.close()
                return 0

            if mode == "probe":
                got = find_input(page)
                log("  输入框：%s" % ("有" if got else "无"))
                results.append((url, "has-input" if got else "no-input"))
                shot(page, "probe_%d.png" % len(results))
                continue

            ok, how = try_send(page, message)
            log("  发送动作：%s (%s)" % (ok, how))
            if ok:
                shot(page, "after_send.png")
                if verify(page, message):
                    log("  ✅ 已确认消息上屏")
                    sent = True
                    results.append((url, "sent"))
                    break
                log("  ⚠️ 未能在页面上回读到消息，可能未真正发出")
                results.append((url, "unverified"))
            else:
                results.append((url, "send-failed"))
            shot(page, "result_%d.png" % len(results))

        log("各入口结果：" + json.dumps(results, ensure_ascii=False))
        browser.close()

        if mode == "probe":
            return 0 if any(r[1] == "has-input" for r in results) else 1
        return 0 if sent else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", default=None)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()

    mode = "send"
    if a.check:
        mode = "check"
    elif a.probe:
        mode = "probe"

    if mode == "send" and not a.message:
        log("❌ --message 不能为空")
        return 1
    return run(a.message, mode)


if __name__ == "__main__":
    sys.exit(main())
