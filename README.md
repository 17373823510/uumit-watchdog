# UUMit Watchdog

7×24 小时盯着 UUMit，一旦有**接单申请被通过**或**新订单生成**，就用浏览器登录 WorkBuddy 网页端，往会话里发一条约定好的暗号，把 Agent 唤醒去干活。

沙箱里做不到这件事（沙箱一休眠进程全死，"自己叫醒自己"在架构上不成立），所以把轮询搬到 GitHub Actions：**公开仓库 Actions 免费、无限额**，正好当常驻机器用。

---

## 一、它是怎么跑起来的

```
┌──────────── GitHub Actions（公开仓库，免费常驻）────────────┐
│                                                              │
│  watch.yml  cron */15min 触发                                │
│      │                                                       │
│      ├─ 自检：已有实例在跑 → 本次立即退出（防并发）            │
│      │                                                       │
│      └─ 无实例 → 长驻循环（最多 5.5h，job 硬上限 6h）          │
│            每 POLL_INTERVAL 秒：                              │
│              1. GET /api/v1/tasks/applications/mine           │
│              2. GET /api/v1/orders?role=seller                │
│              3. 与 state/seen.json 比对                       │
│              4. 有变化 ──► Playwright 打开 WorkBuddy ──► 发暗号 │
│                              │                               │
│                              └─ 失败 ──► 手机推送兜底通知      │
└──────────────────────────────────────────────────────────────┘
                          │ 暗号消息
                          ▼
                  Agent（我）被唤醒 ──► 读 state/pending.json ──► 开工
```

**为什么 job 要长驻而不是纯 cron**：GitHub 的 `schedule` 不保证准点，实测延迟 5~30 分钟很常见。让单个 job 在内部循环 5.5 小时，实际检测间隔就是 `POLL_INTERVAL`（默认 60 秒），接近实时。

**状态怎么跨 job 存活**：runner 是临时的，状态存 `state/`，靠 `actions/cache` 在主 job 之间传递，同时有变化时 commit 回仓库。

**数据怎么给到 Agent**：待办会写成 `state/pending.json`，唤醒消息里直接带上 raw 链接：
`https://raw.githubusercontent.com/<owner>/<repo>/main/state/pending.json`
公开仓库可直接匿名拉取，Agent 拿到就能开工，不依赖沙箱里的旧文件。

---

## 二、部署步骤

### 1. 建公开仓库并推送

```bash
cd uumit-watchdog
git init && git add -A && git commit -m "init: uumit watchdog"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> 必须是 **Public**。私有仓库的 Actions 有月度额度限制，公开仓库免费无限。
> 代码里没有任何凭据，敏感值全部走 Secrets。

### 2. 配 Secrets

`Settings → Secrets and variables → Actions → New repository secret`：

| Secret | 必需 | 说明 |
|---|:--:|---|
| `UUMIT_API_KEY` | ✅ | UUMit API Key |
| `UUMIT_USER_ID` | ✅ | `platform_user_id`（UUID） |
| `WB_STORAGE_STATE_B64` | 推荐 | Playwright storage_state 的 base64，比裸 cookie 耐用 |
| `WB_COOKIE` | 二选一 | Cookie 字符串（F12 复制的整行） |
| `PUSHPLUS_TOKEN` | ✅ | PushPlus 令牌，兜底通知走微信 / QQ / 短信 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 可选 | Telegram 备用 |
| `BARK_KEY` / `SERVERCHAN_KEY` / `NTFY_TOPIC` | 可选 | 其它备用通道 |

对应的 **Variables**（非敏感）：`PUSHPLUS_CHANNELS`（默认 `wechat,qq,sms`）、`POLL_INTERVAL`、`WAKE_COOLDOWN`、`WB_CHAT_URL`、`UUMIT_BASE_URL`。

#### 怎么拿到 UUMit 凭据

在已经接入 UUMit 的机器上：

```bash
cat ~/.codebuddy/skills/uumit-agent/memory/uumit-auth.json
# {"api_key": "...", "platform_user_id": "..."}
```

#### 怎么拿到 WorkBuddy 凭据

> ⚠️ **关键结论（2026-10-12 实测）**
> - `/chat/` 入口：登录态正常，但服务端已关闭「发起新对话」，页面直接提示停用 → **发不出去**。
> - `/app` 入口：没有停用提示，是目前唯一的希望，但它用的是 Keycloak 里**另一个 OIDC client**（`platform=agents`），
>   和 `/chat/`（`platform=website`）**登录态不互通，SSO 也不会自动跳转**，必须单独登录一次。
>
> 所以抓凭据时请务必用 **`--platform agents`**（脚本默认值）。

**方式 A（推荐）：抓 storage_state**

在本仓库目录下跑一次（需要 `pip install playwright && playwright install chromium`）：

```bash
# 默认抓 /app 这个入口
python scripts/login_capture.py --phone 17373823510

# 万一想抓 /chat/（已知停用，仅备用）
python scripts/login_capture.py --phone 17373823510 --platform website
```

脚本会：点掉协议弹窗 → 切手机号登录 → 填号 → 等你输入短信验证码 → 提交 →
回目标入口验证 → 顺便告诉你这个入口**能不能发新对话** → 打印 base64，直接粘进 `WB_STORAGE_STATE_B64`。

**方式 B：手动复制 cookie**

用浏览器打开 `https://www.workbuddy.cn/app`（不要用 /chat/）→ 登录 →
F12 → Network → 随便点一个请求 → Request Headers → 复制 `cookie` 整行的值 → 粘进 `WB_COOKIE`。

### 3. 先手动验证一遍

`Actions → UUMit Watchdog → Run workflow`，依次跑三种模式：

| mode | 作用 | 期望结果 |
|---|---|---|
| `check` | 登录态是否有效 | 日志出现 `✅ 已登录` |
| `probe` | 探测哪个页面能发新对话 | 出现 `has-input`；同时下载 `wakeup-shots-*` 产物看截图 |
| `once` | 真跑一次检测 | 申请数/订单数打印出来，无事件则 `新事件 0` |

> ⚠️ `probe` 这一步很关键。WorkBuddy 网页端的 `/chat/` 曾经关掉过"发起新对话"，如果所有入口都返回 `no-input` 或 `disabled`，浏览器唤醒这条路就是不通的 —— 此时只能靠兜底通知提醒你手动发一句话。脚本会自动 fallback 到通知，不会静默失败。

---

## 三、暗号约定

唤醒消息固定以 **`AUTO`** 开头（换行后跟任务清单）。Agent 侧收到 `AUTO` 开头即进入自动模式：直接开工，且回复压在 10 行内，省额度。

这条约定同时写在项目根目录的 `CLAUDE.md` 里，任何新会话都会自动加载。

---

## 四、7×24 的保障与已知风险

| 风险 | 表现 | 应对 |
|---|---|---|
| **WorkBuddy cookie 过期** | `logged-out`，唤醒失败 | 兜底通知会提醒你；重跑 `login_capture.py` 更新 Secret。优先用 `WB_STORAGE_STATE_B64`，它带 localStorage 刷新令牌，活得比裸 cookie 久 |
| **GitHub schedule 延迟/不触发** | 日志间隔忽长忽短 | 已用 job 内长驻循环规避大部分；极致要求见下方「外部 cron 加固」 |
| **仓库长期无活动导致 schedule 被禁用** | 突然不再触发 | `heartbeat.yml` 每天做一次空提交保活；同时每天自检 UUMit 凭据 |
| **runner 出口 IP 被风控** | 登录态检查不通过 | 换用 `WB_CHAT_URL` 直连会话页；或走兜底通知人工介入 |
| **公开仓库日志可见** | Actions 日志任何人都看得到 | 只打印任务标题等公开市场信息；凭据全部走 Secrets，GitHub 自动打码 |

### 外部 cron 加固（可选）

GitHub 的 `schedule` 终究是"尽力而为"。要真正做到分钟级不中断，可以用免费外部定时器（cron-job.org / Cloudflare Workers Cron Triggers）每 5 分钟打一次 `workflow_dispatch`，完全绕开 GitHub 的调度：

```bash
curl -X POST \
  -H "Authorization: Bearer <你的 PAT，需 repo 权限>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/actions/workflows/watch.yml/dispatches \
  -d '{"ref":"main","inputs":{"mode":"once"}}'
```

配合 job 内的"已有实例在跑就退出"自检，怎么打都不会并发。

---

## 五、目录结构

```
uumit-watchdog/
├── .github/workflows/
│   ├── watch.yml        主看门狗：检测 + 唤醒 + 兜底通知 + 状态回写
│   └── heartbeat.yml    每日保活 + 凭据自检
├── scripts/
│   ├── uumit_client.py  UUMit 只读客户端（零依赖）
│   ├── watch.py         主编排：差异检测 → 唤醒 → 通知
│   ├── wakeup.py        Playwright 浏览器唤醒（send/check/probe）
│   ├── notify.py        兜底通知（Telegram/Bark/Server酱/ntfy）
│   └── login_capture.py 交互登录一次，导出 storage_state 的 base64
├── state/
│   ├── seen.json        已见过的申请/订单（跨 job 状态，走 cache）
│   └── pending.json     当前待办，供 Agent 用 raw 链接拉取
└── .env.example         所有环境变量的键名清单
```

## 六、本地调试

```bash
pip install -r requirements.txt
export UUMIT_API_KEY=... UUMIT_USER_ID=...
python scripts/uumit_client.py          # 自检连通性
python scripts/watch.py --once --dry-run   # 只检测不唤醒
python scripts/watch.py --loop --interval 60 --max-seconds 600
```
