#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备用通知通道
============
浏览器唤醒失败时的兜底：把告警推到手机上，由人工发一句话唤醒 Agent。

已支持（配置了哪个就用哪个，都没配就静默跳过）：
    PushPlus   PUSHPLUS_TOKEN    （主用；PUSHPLUS_CHANNELS 指定渠道，默认 wechat,qq,sms）
    TELEGRAM   TG_BOT_TOKEN + TG_CHAT_ID
    Bark(iOS)  BARK_KEY          （可选 BARK_SERVER，默认 https://api.day.app）
    Server酱   SERVERCHAN_KEY    （SendKey，sctapi.ftqq.com）
    ntfy       NTFY_TOPIC        （可选 NTFY_SERVER，默认 https://ntfy.sh）

零第三方依赖。全部走环境变量。
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

PUSHPLUS_API = "http://www.pushplus.plus/send"

import json
import os
import urllib.error
import urllib.parse
import urllib.request


def _post(url, data=None, headers=None, timeout=20):
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8") if isinstance(data, (dict, list)) \
            else urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception as e:                               # noqa: BLE001
        print("[notify] %s 失败: %s" % (url.split("/")[2], e), flush=True)
        return False


def send(title, content, channels=None):
    """返回成功投递的通道名列表。"""
    ok = []
    want = set((channels or "").split(",")) if channels else None

    # ---- PushPlus（微信 / QQ / 短信 / 邮件 / 企业微信，一个 token 多渠道）----
    pplus = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if pplus and (want is None or "pushplus" in want):
        ch_raw = os.environ.get("PUSHPLUS_CHANNELS", "wechat").strip()
        chans = [c.strip() for c in ch_raw.split(",") if c.strip()]
        for ch in chans:
            # 短信有长度成本，砍短；微信/QQ 保留完整内容
            body = content if ch != "sms" else content[:300]
            payload = {
                "token": pplus,
                "title": title[:100],
                "content": body,
                "template": "txt",
                "channel": ch,
            }
            if _post(PUSHPLUS_API, payload, {"Content-Type": "application/json"}):
                ok.append("pushplus:" + ch)

    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if token and chat and (want is None or "telegram" in want):
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        if _post(url, {"chat_id": chat, "text": title + "\n\n" + content,
                       "disable_web_page_preview": "true"},
                 {"Content-Type": "application/x-www-form-urlencoded"}):
            ok.append("telegram")

    bark = os.environ.get("BARK_KEY", "").strip()
    if bark and (want is None or "bark" in want):
        server = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
        if _post("%s/%s/%s/%s" % (server, bark,
                                  urllib.parse.quote(title),
                                  urllib.parse.quote(content[:1500]))):
            ok.append("bark")

    sc = os.environ.get("SERVERCHAN_KEY", "").strip()
    if sc and (want is None or "serverchan" in want):
        if _post("https://sctapi.ftqq.com/%s.send" % sc,
                 {"title": title, "desp": content},
                 {"Content-Type": "application/x-www-form-urlencoded"}):
            ok.append("serverchan")

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic and (want is None or "ntfy" in want):
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        if _post("%s/%s" % (server, topic),
                 (title + "\n" + content).encode("utf-8"),
                 {"Title": title.encode("utf-8").decode("latin-1", "replace"),
                  "Content-Type": "text/plain; charset=utf-8"}):
            ok.append("ntfy")

    print("[notify] 已投递: %s" % (ok or "无（未配置通道）"), flush=True)
    return ok


if __name__ == "__main__":
    import sys
    send(sys.argv[1] if len(sys.argv) > 1 else "UUMit 测试",
         sys.argv[2] if len(sys.argv) > 2 else "这是一条测试通知")
