# 部署说明

## 缺少什么（先说清楚）

要长期、稳定地给 iPhone Safari 用，需要同时满足：

1. **能运行浏览器的服务器**：Playwright + Firefox（注意：不是 Chromium——Cloudflare 会识别无头 Chromium，见下文“403 的真正原因”），约 2GB 共享内存，首次启动较慢。纯静态托管（GitHub Pages、Vercel 静态、Muse 静态页面）**不能**跑浏览器后端，不满足。
2. **公网入站 HTTPS 地址**：手机能直接打开的网址。
3. **持续运行**：任务是后台异步跑的，服务器不能随请求结束就停。

**本机 Muse VM 不满足 2**：它没有可公开访问的入站端口和域名，也没有已连接的 Render / Railway / Fly.io 账号。本仓库因此提供完整部署配置，但“已部署成长期公开网址”这一步，需要你在下面任选一种方式完成（约 10–20 分钟），或给我一个已有账号的部署目标。

## 方式 A：Render（推荐，最省事）

1. 把本仓库推到 GitHub（公开或私有均可）。
2. Render → New → Blueprint → 选择该仓库，`render.yaml` 已写好 Docker 部署。
3. 计划：`render.yaml` 现在写的是 **free（免费）**，按用户 2026-11-12 的选择。免费版 15 分钟无访问就休眠，冷启动浏览器可能要 1–2 分钟，内存小、偶尔可能启动失败；如需更快更稳，可再改回 Standard（约 $7/月起）。
4. 部署完成后打开 Render 给的 `https://xxx.onrender.com`，即为手机可用网址。
5. 健康检查：`https://xxx.onrender.com/api/health` 应返回 `{"ok": true}`。

## 方式 B：Fly.io / Railway（Docker）

- Fly.io：`fly launch`（选 Dockerfile），`fly deploy`；需要信用卡验证账号。
- Railway：New Project → Deploy from GitHub Repo，选 Dockerfile；按量计费。
- 两者都不需要改代码，`Dockerfile` 已用 Playwright 官方镜像，并额外装了 Firefox。

## 方式 C：自己的 VPS / 服务器

```bash
docker compose up -d --build
# 打开 http://服务器IP:8000/（生产请配 Nginx + HTTPS + 域名）
```

要求：2核 4GB 以上内存，`shm_size: 2gb` 已在 compose 里写好。

## 方式 D：临时公开网址（仅用于一次实测，不作长期部署）

在已跑起本地服务的机器上，用 Cloudflare Tunnel 的临时隧道：

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# 得到一个 https://xxx.trycloudflare.com 临时网址，手机可打开
```

注意：临时隧道地址会变、会过期，不适合长期使用；也不要把它当成正式部署。

## 无真实 session 测试（部署后先做这些，再用真实 session）

1. 打开首页，粘贴一个明显无效的短字符串 → 应返回 400，提示 session 过短。
2. 粘贴一个格式正确但已过期的 session → 任务应以 `login_required`（跳回登录页）或 `bot_check` / `access_denied_403` 失败，**不应**出现任何订阅或账单。
3. 用两个不同任务的文件 token 互相访问 → 应返回 403。
4. 等 30 分钟后再次访问旧任务 → 应返回 404（已清理）。

只有第 2 步换成**有效 session**后，看到真实截图、且至少下载到一份 `%PDF` 开头的原始发票 PDF，才算“真实账户端到端验证通过”。

## 常见失败与含义

| 失败阶段 | 含义 |
|---|---|
| `login_required` | session 过期 / 缺分段 / 不被接受，页面跳回登录页 |
| `bot_check` | 遇到 Cloudflare / Turnstile 人机验证，不自动解 |
| `access_denied_403` | chatgpt.com 返回 403，服务端拒绝本次访问（首次打开即 403 时会自动重试一次；仍 403 则如实失败） |

## 403 的真正原因（2026-11-12 实测）

最初以为是机房 IP 被封：Render 弗吉尼亚、Render 新加坡、租用的腾讯云服务器（43.128.86.133）三次实测都在首页 403。但在该服务器上进一步测试发现：

- 不用浏览器、只用普通 HTTPS + Chrome UA 请求 `chatgpt.com` → **200**；
- 同一 IP 上 Playwright 无头 Chromium 打开首页 → **403**，标题是 Cloudflare 的 "Just a moment..."；
- 同一 IP 上 Playwright Firefox（配 Firefox UA）打开首页 → **200**，是真实首页。

所以不是 IP 被硬封，而是 **Cloudflare 识别出了无头 Chromium**。修复方法是改用 Firefox（`app/browser_runner.py` 已改，`Dockerfile` 已装 Firefox）。如果将来 Firefox 也被识别，下一招是带虚拟显示器的有头模式（Xvfb），而不是继续换 IP。

## 工作空间账单路径（Business，2026-11-12）

个人 Billing 页（Settings → Billing → Transaction history）对 Business 工作空间没有发票：工作空间的发票在 **Workspace settings → Billing → Invoices**，只有所有者能看到；管理员只能看套餐、看不到发票（OpenAI Help Center《Managing billing for ChatGPT and the API platform》《Managing billing and seats in ChatGPT Business》）。有些工作空间点 Workspace settings 会自动跳到 Admin Console（`chatgpt.com/admin` → 选工作空间 → Billing），所以工具按顺序试两条路：先 Admin Console，再 `#settings/Workspace`。个人账单没有下载到 PDF 时才走这条路；抓到的订阅信息会并进"工作空间账单（Stripe 门户）"卡片。注意：工具用 session 打开的 Stripe 门户是**个人账号**的客户门户（空），不是工作空间的——不要把门户为空当成"工作空间没有账单"。
| `browser_error` | 服务器 Chromium 启动失败，检查内存 / 镜像 |
| 账单为空但截图正常 | 可能是 Apple / Google Play 订阅（发票在应用商店），或页面结构变了；以截图为准，不要凭空补数据 |
