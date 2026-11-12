# 已知问题：工作空间账单路径仍未跑通（2026-11-12）

> 给接手的 agent：请按本文修，不要重新验证“已确认的事实”一节。
> 原计划同时建一个 GitHub Issue，但两次写入都因审批卡片超时未执行；
> 若 Issue 已建，以 Issue 为准，本文可删。

## 目标（不要改）

查**当前这个号（jery）的 Business 工作空间订阅**，不是 ₱982.14 那张 Plus 发票——那张是另一个账号的，已排除。

官方路径（OpenAI Help Center《Managing billing for ChatGPT and the API platform》
《Managing billing and seats in ChatGPT Business》）：Workspace settings →
Billing → Invoices，只有工作空间所有者能看到发票；部分工作空间点 Workspace
settings 会自动跳 Admin Console（chatgpt.com/admin → 选工作空间 → Billing）。

当前部署：http://43.128.86.133:8000/ ，版本 `1d0a1270`（/api/version 已核实），Firefox 后端。

## 本次实测证据（2026-11-12 18:11–18:13 CST）

- 用户用同一 session 在部署站实测，结果与之前一样：0 条账单记录、0 张发票 PDF、订阅信息未识别。
- 服务器任务目录 `data/jobs/b1bd8139b68236d3be7ec11423d82a2f/`（30 分钟 TTL，过期自动删除）里只有 3 个文件：
  - `billing.png`（31,790 字节，18:11）与 `subscription.png`（31,790 字节，18:13）：
    **都是 Cloudflare “Verify you are human” 验证页**，不是设置页。
  - `stripe-portal.png`（67,672 字节，18:13）：门户打开了，显示 No invoice history，账单姓名 jery。
- 结果消息原文（节选）：`完成：找到 0 条账单记录，其中 0 张已下载原始发票 PDF，订阅信息
  页面上未识别到。个人 Billing 面板没有加载出来：截图里是 General 设置页，且设置侧栏里没有
  Billing/Subscription 入口。Business 工作空间的账单不在这里，在工作空间账单路径里
  （见工作空间账单结果）。Stripe 门户里也显示没有发票记录（No invoice history）。门户账单姓名：jery。`
- 任务目录里**没有 `workspace-billing.png`**，也没有任何发票页截图。
- 截图含账号名，请勿原样上传到公开处；本文只用文字描述证据。

## 问题清单

### P0：Billing / Subscription 页被 Cloudflare 人机验证拦住，45 秒等待没通过

`billing.png` 和 `subscription.png` 证明：`#settings/Billing` 与 `#settings/Subscription`
打开的都是验证页。代码里已有 `_wait_for_challenge_to_clear`（最多等 45 秒，不点验证、不解题），
但这次没等到通过。

工作空间两条路由（`chatgpt.com/admin`、`#settings/Workspace`）很可能同样被验证拦住：
`_fetch_workspace_billing` 里验证未通过就 `continue`，两条路由都没有出现 billing/invoice
文本时 `_parse_workspace_page` 根本不会执行——这与“没有 workspace-billing.png”一致。

修的方向：验证处理需要升级（例如验证通过后再重试导航、等待更久、或在验证清除后重新进入设置页），
并在结果里写明验证在哪一步出现、等了多久、最终是否通过。注意红线：
**不自动点验证、不解 CAPTCHA/Turnstile、不解 proof-of-work**；过不了就如实报 `bot_check`，
不要继续往下走流程假装在查账单。

### P1：失败诊断误报——把验证页说成了 General 页

结果消息说“截图里是 General 设置页”，但实际截图是验证页。原因：`run_job` 里 `done_msg`
的分支只看“无账单内容 + 侧栏无 Billing”就走 General 分支，没有先判断页面是不是验证页
（`_body_signals` 的 `is_challenge` 已有，但该分支没用它）。

修的方向：拼 `done_msg` 前先判 `is_challenge`；是验证页就明确报验证失败（在哪一步、等了多久），
不要报 General 页、不要让用户去看不存在的侧栏。

### P2：工作空间步骤不可观测，失败后无法复核

- `page_debug`（含 `workspace_billing` 的 `found_via_* / challenge / login_required / no_billing_on_*`）
  只存在内存里，任务过期后无从查证。
- 两条工作空间路由即使没命中 billing 文本也不截图，导致本次只能靠“没有 workspace-billing.png”
  反推它没走到解析那一步。

修的方向：把 `page_debug` 和关键 stage 写入任务结果（或随任务文件落盘）；无论是否命中 billing
文本，都保存工作空间路由的截图（如 `workspace-admin.png` / `workspace-settings.png`）；结果消息里
写明工作空间步骤实际走到了哪一步。

### P3（已知，不用重查）：Stripe 门户回退取的是个人客户门户

`stripe-portal.png` 的门户是个人账号的（jery，No invoice history），不是工作空间的。
门户为空不能当成“工作空间没有账单”的证据；工作空间账单只能从工作空间路径拿。
见 `DEPLOY.md`“工作空间账单路径”一节。

## 已确认的事实（不用重新验证）

- 用户是 jery Business 工作空间的**所有者**；个人账户是免费版，个人 Billing 没有账单是正常的。
- 工作空间订阅（用户截图）：ChatGPT Business Subscription US$25.00/月，渠道折扣
  `smb_channel_partner_talen_6ae0ca1f423c8` −US$25.00（折扣至 2030-11-29），合计 US$0.00，
  下一笔付款预计 US$0.00，付款方式 Mastercard •••• 4675，续费日 2026-11-29。
- 因为下一笔是 $0，即使工作空间路径打通，也可能没有可下载的发票 PDF——**验收标准先定为
  “订阅信息可查”（套餐/金额/续费日/付款方式），发票 PDF 有则加分，无则如实说明，不要硬凑**。
- 25 个离线测试全过（含工作空间假页面的行点击 + 同页跳转 + blob PDF 下载），所以本地逻辑没问题，
  缺的是真实页面上的验证。

## 验收

用同一 session 在 http://43.128.86.133:8000/ 实测，页面上能看到 Business 订阅
（套餐、金额、续费日、付款方式），或给出明确、可复核的失败阶段。不要只改提示文案、不要返回入口链接当完成。

## 相关代码位置

- `app/browser_runner.py`：`_fetch_workspace_billing`、`_parse_workspace_page`、
  `_wait_for_challenge_to_clear`、`run_job` 里的 `done_msg` 分支、`_download_invoice_pdfs`。
- `app/static/index.html`：结果消息与步骤展示。
- `DEPLOY.md`：“403 的真正原因”与“工作空间账单路径”两节。
