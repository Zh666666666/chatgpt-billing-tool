# ChatGPT 订阅与账单查询工具

手机浏览器打开网页，粘贴自己账户的 session，点击获取，即可在本页看到真实订阅页面截图和账单，并预览、下载原始发票 PDF。

## 状态（请按这四档区分，不要混淆）

| 档位 | 当前状态 |
|---|---|
| 代码已写好 | ✅ 前端 + 后端 + 部署配置已完成，见本仓库 |
| 模拟测试通过 | ✅ 见下文“本地测试”：session 解析单测、无效 session 的分阶段失败、文件隔离 |
| 已部署 | ⏳ 取决于部署方式，见 DEPLOY.md。**本机 VM 没有公网入站端口，不能直接当作长期公开网址** |
| 真实账户端到端验证通过 | ❌ **尚未验证**。需要一个有效 session 在部署好的实例上实测，拿到真实截图和至少一份原始 PDF 后才能打勾。在此之前，本工具不能描述为“已完成” |
| 本机实测结果 | ⚠️ 本机 VM 上：12 个单测全部通过；FastAPI 首页 / 健康检查 / 凭据校验正常；用假 session 建任务后，浏览器在 `opening_chatgpt` 阶段明确失败：`net::ERR_TUNNEL_CONNECTION_FAILED`，无任何伪造订阅/账单。原因是本机出口代理拒绝 Chromium 的 HTTPS 隧道（curl / Python 直连是通的），见下文“本地限制” |

## 为什么不用云端直接调接口

此前方案云端直接调用账户、订阅、账单内部接口，全部返回 HTTP 403 HTML。本工具**完全不走这条路**，不做任何到 `chatgpt.com/backend-api` 的服务端直连。

唯一路径是真浏览器（Playwright Firefox——Cloudflare 会识别无头 Chromium，见 DEPLOY.md“403 的真正原因”）：

```
chatgpt.com → Settings → Billing → Transaction history
  → invoice.stripe.com → Download invoice（原始 PDF）
回退：Settings → Subscription → Manage my subscription
  → billing.stripe.com 门户 → Invoice history → Download PDF
```

同时，GitHub 搜索结论（2026-11-12，2026-11-12 再次用 GitHub 连接器核实了仓库 README 与源码）：没有找到同时满足“手机网页 + 粘贴 session + 截图 + 账单列表 + 页内原始 PDF 下载”的现成开源项目。最接近的 multivibe 只给入口链接；Usage-Monitor / omnics/InvoiceManager 的结论是 ChatGPT 消费订阅没有公开账单 API；quota-control 是桌面托盘应用，只给续费日期，没有发票 PDF；Reduck 是商业产品，需桌面 Chrome。

## GitHub 上可复用的部分（已并入本工具）

核实过的 3 个发票下载器仓库：

- [chrismazanec/openai-invoice-downloader](https://github.com/chrismazanec/openai-invoice-downloader)（3★，Python + Playwright）：**已采用其模式，重新实现，未直接复制代码**（该仓库无 LICENSE 文件）。包括：浏览器内 `fetch('/api/auth/session')` 取 access token、再浏览器内 `fetch('/backend-api/payments/customer_portal')` 取 Stripe 门户地址——只在 UI 未找到账单时作回退，且 token 永不返回给后端、不记日志；Stripe 门户发票链接选择器 `a[data-testid="hip-link"]`；发票页用 `button.Button--primary` + `expect_download` 下载，失败时拦截 `application/pdf` 响应作回退；日期首行解析成 `YYYY-MM-DD` 文件名。**未采用**：持久 Chrome profile + 手动登录（手机粘贴 session 的流程用不上）。
- [tedbibr/chatgpt-invoice-downloader](https://github.com/tedbibr/chatgpt-invoice-downloader)（1★）：已采用其 Stripe 日期两种格式解析（`Feb 12, 2026` 与 `12 Feb 2026`）、`a[href*='invoice']` 回退选择器、Settings → Account → 第二个 Manage 按钮进门户的导航思路。**未采用**：填邮箱 + 等验证码登录（用户只有 session，不提供邮箱/验证码）。
- [Iwamo-yu/chatgpt-invoice-downloader](https://github.com/Iwamo-yu/chatgpt-invoice-downloader)（0★，MIT）：已采用其思路——按发票号去重、App Store / Google Play 走不同账单流程、session 过期属正常现象需重新准备。**未采用**：本地 Chrome storage state / `prepare-session` / Google Drive 上传（手机流程用不上）。
- 另外两个参考：[DurinDoor 的 chatgpt-web 文档](https://www.durindoor.app/docs/providers/chatgpt-web)确认了分段 session cookie 必须按原名回传、绝不拼接；[quota-control](https://github.com/buidangminh23/quota-control) 的“只有页面明确确认的续费/付费日期才算数”原则已用于订阅提取（带 `observed_at`）。

## 凭据能力（不要互换）

- **Session cookie（推荐）**：`__Secure-next-auth.session-token`，或分段 `.0` / `.1` / …，或整条 Cookie 请求头。分段必须按原名回传，**绝不拼接成一个值**；同时有完整值和分段时，丢弃可能过期的完整值。这是唯一能让浏览器登录并打开 Billing / Stripe 门户的凭据。
- **Session JSON**：浏览器导出的 cookies 数组，从中只提取 session cookie；没有它就报错。
- **Access token**：**不能替代 session cookie**。它无法让浏览器登录 chatgpt.com，也打不开 Billing / Stripe 门户。提交后会直接返回能力错误，不启动浏览器。
- 不需要账号密码、验证码、OpenAI API Key、Stripe 密钥。

## 本地限制（本机 VM，实测发现）

- 本机没有公网入站端口，不能直接当作长期公开网址。
- 本机出口代理拒绝 Chromium 的 HTTPS 隧道：`curl https://chatgpt.com/` 返回 200，但 Playwright Chromium 打开任何 https 站都报 `net::ERR_TUNNEL_CONNECTION_FAILED`（已试 Playwright proxy 配置、launch 参数代理、忽略证书错误三种方式，均失败）。Chromium 本身是好的：离线 `set_content` + 截图测试通过。
- 因此**本机只能做到**：代码、单测、离线浏览器测试、API 校验、假 session 的分阶段失败。**不能在本机完成真实账户端到端验证**，也不能在本机放一个“能用的公开网址”——放出来也会在打开 ChatGPT 这一步失败，那会误导你。
- `browser_runner.py` 已加入 `_proxy_config()`：部署环境若有 `HTTPS_PROXY`，会自动传给 Playwright；直连环境（Render / Fly / VPS）无代理变量时不走代理。
- 真实验证必须在有直连互联网的部署环境（Render / Fly / VPS，见 DEPLOY.md）上，用有效 session 跑一次。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install firefox chromium
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000/
```

## 本地测试

```bash
.venv/bin/python -m unittest discover -s tests -v
# 另见 DEPLOY.md 的“无真实 session 测试”：用无效 / 过短 session
# 应得到分阶段失败（login_required / bot_check / access_denied_403），
# 而不是伪造的订阅或账单。
```

## 隐私与隔离

- session 只在 POST JSON 体里提交，不出现在 URL、源码、日志里；只保存在任务运行期间的内存中，浏览器上下文关闭即丢弃，**不写入磁盘**。
- 每个任务一个随机目录 `data/jobs/<job_id>/`（0700，文件 0600），文件访问需该任务的短期 `file_token`（HMAC 比对），A 任务的 token 打不开 B 任务的文件。
- 任务与文件保留 **30 分钟**，到期自动清理；也可点“删除本次任务与文件”立即清理。
- 不执行购买、付款、取消订阅、修改账户等操作，只导航、截图、下载发票。
- 遇到人机验证（Cloudflare / Turnstile）、登录跳转、403 时，明确报告失败阶段，**不绕过、不自动解验证**。

## 部署

见 [DEPLOY.md](DEPLOY.md)：Docker / docker-compose、Render Blueprint、Fly.io、VPS + Cloudflare Tunnel（临时公开网址）。长期公开部署需要一台能运行 Firefox 的服务器或浏览器托管服务，本机 Muse VM 不满足该条件，详见 DEPLOY.md 的“缺少什么”一节。

## 文件结构

```
app/
  main.py            FastAPI：任务 API + 文件 token + 静态前端
  browser_runner.py  Playwright 真浏览器：Billing → Stripe 发票页 → PDF
  session_parse.py   session cookie / JSON 解析（分段不拼接）
  store.py           任务隔离存储、30 分钟 TTL、清理
  static/index.html  手机端页面
tests/test_session_parse.py
Dockerfile  docker-compose.yml  render.yaml  DEPLOY.md
```
