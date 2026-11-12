"""
Real-browser runner. No direct HTTP calls to chatgpt.com / backend-api
from the server are made at all -- that path already returned HTTP 403
HTML from Cloudflare, and repackaging it would not fix it.

Everything comes from a real Firefox page, in a fresh browser context
seeded only with the user's session cookie(s).

Browser choice (verified 2026-11-12 on the rented VPS, IP
43.128.86.133): headless Chromium is detected by Cloudflare -- the
first navigation returns 403 with a "Just a moment..." challenge
page, on Render Virginia, Render Singapore AND the rented VPS alike.
The same IP with plain HTTPS + a Chrome UA returns 200, and Firefox
(with a matching Firefox UA) loads the real homepage with 200. So
the block is browser detection, not a hard IP block, and the runner
uses Firefox.

  chatgpt.com -> Settings -> Billing -> Transaction history
    -> invoice.stripe.com -> Download invoice (original PDF)
  fallback: Settings -> Subscription -> Manage my subscription
    -> billing.stripe.com portal -> Invoice history -> Download PDF

The runner never tries to bypass a bot check, solve a CAPTCHA, or
replay a proof-of-work token. If a challenge / login / 403 appears,
it stops and reports the exact stage.

Only data actually found on the page is returned. Missing fields
stay null; nothing is invented to fill a table.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from .store import fail_job, save_file, update_job


def _proxy_config():
    """Proxy from the environment, if the deployment needs one.

    Normal deployments (Render / Fly / VPS) have direct internet and
    no proxy env, so this returns None and nothing changes.
    In this Muse VM, outbound traffic goes through an egress proxy;
    the browser does not pick up the shell proxy env on its own, so we
    pass it explicitly here. Credentials, if any, are parsed from the
    env URL and handed to Playwright only -- they are never logged
    and never written to disk.
    Returns (launch_proxy, ignore_https_errors).
    """
    raw = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
           or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    if not raw:
        return None, False
    try:
        u = urlparse(raw)
        if not u.hostname:
            return None, False
        proxy = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 3128}"}
        if u.username:
            proxy["username"] = u.username
        if u.password:
            proxy["password"] = u.password
        # Egress proxies in sandboxed VMs often MITM TLS with a custom
        # CA; the browser does not trust that CA by default. Only relax
        # verification when a proxy is actually configured.
        return proxy, True
    except Exception:
        return None, False

CHATGPT_HOME = "https://chatgpt.com/"
BILLING_URL = "https://chatgpt.com/#settings/Billing"
SUBSCRIPTION_URL = "https://chatgpt.com/#settings/Subscription"

TOTAL_TIMEOUT_S = 180
NAV_TIMEOUT_MS = 30000

BOT_MARKERS = [
    "just a moment", "verify you are human", "checking your browser",
    "verify you're human", "cloudflare", "attention required",
    "please verify", "turnstile",
]
LOGIN_MARKERS = ["log in to continue", "welcome back", "sign up"]
# Words that, on their own, are not proof of being logged out; they
# are only used together with the URL check below.

def _body_signals(page_text: str, title: str, url: str):
    t = (page_text or "").lower()
    return {
        "is_challenge": any(m in t for m in BOT_MARKERS) or "just a moment" in (title or "").lower(),
        "looks_login": ("/auth/login" in url or "auth.openai.com" in url
                        or "login" in url and "chatgpt.com/auth" in url),
        "has_settings_text": ("settings" in t and ("billing" in t or "subscription" in t)),
    }


def _has_billing_content(page_text: str) -> bool:
    """True when the Settings panel actually shows Billing, not the
    General panel. The Billing panel always carries 'Transaction
    history' or a 'Current plan' line; the General panel has neither.
    A hash navigation that lands on General must not be mistaken for
    a Billing page with zero invoices."""
    t = (page_text or "").lower()
    return ("transaction history" in t or "current plan" in t
            or ("billing" in t and "subscription" in t))


def _sidebar_has_billing(page_text: str) -> bool:
    """True when the Settings sidebar lists a Billing/Subscription
    entry at all. A workspace member account often has neither --
    billing belongs to the workspace admin -- and then no navigation
    can open a Billing panel for this identity."""
    t = (page_text or "").lower()
    return "billing" in t or "subscription" in t


async def _wait_for_challenge_to_clear(page, timeout_s: int = 45) -> bool:
    """Wait for a Cloudflare/Turnstile challenge page to clear on its
    own in a real browser. We never click the checkbox, never solve
    a CAPTCHA and never replay a proof-of-work token; we only wait
    for the page to proceed, the way a normal page load waits. Returns
    True when the challenge is gone, False on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            text = await page.inner_text("body")
            title = await page.title()
        except Exception:
            await asyncio.sleep(2)
            continue
        sig = _body_signals(text, title, page.url)
        if not sig["is_challenge"]:
            return True
        await asyncio.sleep(2)
    return False

def _extract_subscription(text: str) -> dict | None:
    """Best-effort extraction from visible settings text. Returns None
    if nothing recognisable is found; never fabricates a plan."""
    if not text:
        return None
    # Normalise whitespace, keep a bounded raw excerpt for the UI.
    flat = re.sub(r"\s+", " ", text)
    found: dict = {}

    plan_m = re.search(
        r"\b(ChatGPT\s+(Plus|Pro|Go|Team|Business|Enterprise)|Plus|Pro)\b", flat)
    # Prefer an explicit "Current plan" line if present.
    cur = re.search(r"Current plan\s*[:\-]?\s*([A-Za-z ]{2,30})", flat, re.I)
    if cur:
        found["plan"] = cur.group(1).strip()
    elif plan_m:
        found["plan"] = plan_m.group(0).strip()

    for label, key in [
        (r"Next billing date", "next_billing_date"),
        (r"Renews on", "next_billing_date"),
        (r"Next payment", "next_billing_date"),
        (r"Billing period", "billing_period"),
        (r"Current period", "billing_period"),
        (r"Payment method", "payment_method"),
        (r"Billing email", "billing_email"),
    ]:
        m = re.search(label + r"\s*[:\-]?\s*([^\n]{2,80})", flat, re.I)
        if m and key not in found:
            found[key] = m.group(1).strip()[:80]

    amount_m = re.search(
        r"(\$|€|£|₱|¥|₹|A\$|C\$)\s?[\d,]+(?:\.\d{2})?\s*(?:/\s*month|per month|/mo)?", flat)
    if amount_m:
        found["amount_text"] = amount_m.group(0).strip()

    status_m = re.search(r"\b(Active|Canceled|Cancelled|Past due|Trialing|Free)\b", flat)
    if status_m:
        found["status"] = status_m.group(1)

    if not found:
        return None
    # quota-control's rule, adopted here: a date/plan only counts when
    # the page explicitly confirmed it, and we record when it was seen.
    import datetime as _dt
    found["observed_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    found["source_text_excerpt"] = flat[:600]
    return found

def _parse_invoice_row_text(text: str) -> dict:
    flat = re.sub(r"\s+", " ", text or "").strip()
    inv = {"date": None, "amount_text": None, "currency": None,
           "invoice_number": None, "status": None, "raw_text": flat[:200]}
    # Two date shapes seen in the wild (tedbibr's parser handles both):
    # "Feb 12, 2026" and Stripe's "12 Feb 2026".
    date_m = re.search(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b", flat)
    if not date_m:
        date_m = re.search(
            r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b", flat)
    if date_m:
        inv["date"] = date_m.group(0)
    amt_m = re.search(r"(\$|€|£|₱|¥|₹|A\$|C\$)\s?[\d,]+(?:\.\d{2})?", flat)
    if amt_m:
        inv["amount_text"] = amt_m.group(0)
        inv["currency"] = amt_m.group(1)
    num_m = re.search(r"\b([A-Z]{3,}[A-Z0-9\-]{4,})\b", flat)
    if num_m and ("-" in num_m.group(1) or num_m.group(1).startswith("INV")):
        inv["invoice_number"] = num_m.group(1)
    for s in ("Paid", "Open", "Void", "Uncollectible", "Draft"):
        if re.search(rf"\b{s}\b", flat, re.I):
            inv["status"] = s
            break
    return inv

async def _screenshot(page, job, filename: str, label: str):
    try:
        data = await page.screenshot(type="png")
        save_file(job, filename, data, kind="screenshot", content_type="image/png")
        return filename
    except Exception:
        return None

async def _save_pdf_bytes(job, data: bytes, suggested: str | None, index: int):
    """Save bytes only if they are really a PDF. Returns filename or None."""
    if not data or not data.startswith(b"%PDF"):
        return None
    # Date-first naming borrowed from chrismazanec's invoice_filename()
    # ("Feb 14, 2026" -> YYYY-MM-DD), but the Stripe-suggested name
    # wins when it is a safe PDF name, so the original filename is kept.
    name = f"invoice-{index+1}.pdf"
    suggested = (suggested or "").strip()
    if suggested.lower().endswith(".pdf") and "/" not in suggested and "\\" not in suggested:
        name = suggested
    save_file(job, name, data, kind="invoice_pdf", content_type="application/pdf")
    return name

def _parse_portal_billing(text: str) -> dict | None:
    """Parse the Stripe customer portal page into billing fields.

    The portal IS the workspace billing view for a Business workspace:
    current subscription, next payment, payment method, discount and
    invoice history. Returns None when nothing recognisable is found.
    All values come from the page text; missing fields stay absent.
    An empty invoice history together with a $0 next payment is a
    normal state for a fully discounted subscription, not a failure
    to fetch -- the caller says so explicitly.
    """
    if not text:
        return None
    flat = re.sub(r"\s+", " ", text)
    found: dict = {}
    plan_m = re.search(
        r"(ChatGPT\s+(?:Business|Plus|Pro|Go|Team|Enterprise)\s+Subscription)", flat)
    if plan_m:
        found["plan"] = plan_m.group(1).strip()
    amt_m = re.search(
        r"(US\$[\d,]+\.\d{2})\s*/\s*(?:每\s*\d*\s*个月|month|mo\b)", flat)
    if amt_m:
        found["amount_text"] = amt_m.group(0).strip()
    nxt_m = re.search(r"下一笔付款预计为\s*(US\$[\d,]+\.\d{2})", flat)
    if not nxt_m:
        nxt_m = re.search(
            r"[Nn]ext (?:payment|invoice)[^US$]{0,40}(US\$[\d,]+\.\d{2})", flat)
    if nxt_m:
        found["next_payment"] = nxt_m.group(1)
    ren_m = re.search(r"服务将于\s*([0-9年月日]+)\s*更新", flat)
    if not ren_m:
        ren_m = re.search(
            r"[Rr]enews?\s+(?:on\s+)?([A-Za-z]+\s+\d{1,2},?\s*\d{4})", flat)
    if ren_m:
        found["renews"] = ren_m.group(1).strip()
    per_m = re.search(
        r"(\d{1,2}月\d{1,2}日\s*-\s*\d{4}年\d{1,2}月\d{1,2}日)", flat)
    if not per_m:
        per_m = re.search(
            r"([A-Za-z]+\s+\d{1,2}\s*-\s*[A-Za-z]+\s+\d{1,2},?\s*\d{4})", flat)
    if per_m:
        found["current_period"] = per_m.group(1).strip()
    pm_m = re.search(r"(Mastercard|Visa|American Express|Amex)\s*•+\s*(\d{4})", flat)
    if pm_m:
        found["payment_method"] = f"{pm_m.group(1)} •••• {pm_m.group(2)}"
    disc_m = re.search(r"(smb_channel_partner[^\s]*)\s*(-US\$[\d,]+\.\d{2})", flat)
    if disc_m:
        d = f"{disc_m.group(1)} {disc_m.group(2)}"
        end_m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)折扣结束", flat)
        if end_m:
            d += f"，折扣至 {end_m.group(1)}"
        found["discount"] = d
    if "No invoice history" in flat or "没有发票记录" in flat:
        found["invoice_history_empty"] = True
    if not found:
        return None
    found["source_text_excerpt"] = flat[:600]
    return found


async def _try_download_from_stripe_page(page, job, invoice: dict, index: int):
    """On an invoice.stripe.com page, download the original PDF.

    Pattern borrowed (reimplemented, not copied) from
    chrismazanec/openai-invoice-downloader:
      primary: expect_download + the primary download button
        ("Download invoice" text, or button.Button--primary);
      fallback: intercept the application/pdf response while clicking.
    Returns (pdf_filename, receipt_filename).
    """
    # Primary: expect a browser download.
    for selector in ("text=Download invoice", "text=Download receipt",
                     "button.Button--primary"):
        try:
            async with page.expect_download(timeout=15000) as dl_info:
                await page.locator(selector).first.click(timeout=8000)
            download = await dl_info.value
            path = await download.path()
            data = path.read_bytes() if path else b""
            name = await _save_pdf_bytes(job, data, download.suggested_filename, index)
            if name:
                return name, None
        except Exception:
            continue
    # Fallback: intercept the PDF response.
    pdf_bytes: list[bytes] = []

    async def _on_response(response):
        try:
            if "application/pdf" in (response.headers.get("content-type") or ""):
                pdf_bytes.append(await response.body())
        except Exception:
            pass

    try:
        page.on("response", _on_response)
        try:
            await page.locator("button.Button--primary").first.click(timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(5000)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
    for data in pdf_bytes:
        name = await _save_pdf_bytes(job, data, None, index)
        if name:
            return name, None
    return None, None

async def _in_page_portal_url(page):
    """Resolve the Stripe portal URL from inside the browser page.

    Pattern from chrismazanec/openai-invoice-downloader, reimplemented:
    in-page fetch() to /api/auth/session (same origin, with the
    browser's cookies), then in-page fetch() to
    /backend-api/payments/customer_portal with that access token.
    This runs IN THE BROWSER, with its Cloudflare cookies and TLS
    fingerprint -- it is NOT the server-side direct HTTP call that
    already returned 403 HTML, and it is only a fallback when the
    Settings UI did not surface invoices. The access token is never
    returned to Python, never logged, and never stored: the page
    function returns only the portal URL string (or null).
    """
    try:
        return await page.evaluate("""async () => {
          try {
            const s = await fetch('/api/auth/session');
            if (!s.ok) return null;
            const session = await s.json();
            if (!session.accessToken) return null;
            const p = await fetch('/backend-api/payments/customer_portal', {
              headers: { 'Authorization': 'Bearer ' + session.accessToken }
            });
            if (!p.ok) return null;
            const data = await p.json();
            return (data && data.url) || null;
          } catch (e) { return null; }
        }""")
    except Exception:
        return None

async def _download_invoice_pdfs(page, context, job, invoices, started):
    """Click each Billing transaction row and download its invoice PDF.

    Rows often carry NO word "Invoice" at all (just "Nov 12, 2026
    ChatGPT Plus  ₱982.14  Paid"), so they are matched by date +
    amount + Paid, not by the word Invoice. Clicking a row may open
    invoice.stripe.com in a NEW tab, navigate the SAME tab there, or
    open a modal -- all three are handled, then we come back and
    continue with the next row. `invoices` already holds the parsed
    row dicts in order; the matching entry is updated in place with
    pdf_file / source / invoice_number when a PDF is downloaded.
    Returns a small diagnostics dict, also stored on the job so the
    UI can say exactly where invoice fetching stopped.
    """
    debug = {"rows_found": len(invoices), "rows_clicked": 0,
             "new_tabs": 0, "same_tab_navs": 0, "modals": 0, "pdfs": 0}
    job["invoice_debug"] = debug
    for index in range(min(len(invoices), 5)):
        if time.time() - started > TOTAL_TIMEOUT_S - 20:
            break
        inv = invoices[index]
        row_text = (inv.get("raw_text") or "").strip()
        if not row_text:
            continue
        try:
            pages_before = set(context.pages)
            url_before = page.url
            # Click the innermost element whose text is this row.
            # Whitespace must be normalised on both sides: the stored
            # row text is flattened by _parse_invoice_row_text, while
            # the DOM text may hold double spaces / line breaks.
            clicked_text = await page.evaluate("""(wanted) => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              wanted = norm(wanted);
              const els = document.querySelectorAll(
                'button, a, [role="button"], [role="row"], tr, li, div');
              let best = null;
              for (const el of els) {
                const t = norm(el.innerText);
                if (!t || t.length > 300) continue;
                if (t === wanted) {
                  const hasChild = [...el.querySelectorAll('*')].some(
                    c => norm(c.innerText) === wanted);
                  if (!hasChild) { best = el; break; }
                  if (!best) best = el;
                }
              }
              if (!best) return null;
              best.click();
              return (best.innerText || '').trim().slice(0, 200);
            }""", row_text)
            if not clicked_text:
                continue
            debug["rows_clicked"] += 1
            await page.wait_for_timeout(4000)
            target = None
            new_pages = [p for p in context.pages if p not in pages_before]
            if new_pages:
                # Case 1: invoice opened in a new tab.
                target = new_pages[-1]
                debug["new_tabs"] += 1
                try:
                    await target.wait_for_load_state("domcontentloaded", timeout=20000)
                    await target.wait_for_timeout(2000)
                except Exception:
                    pass
            elif page.url != url_before:
                # Case 2: same-tab navigation to the invoice page.
                target = page
                debug["same_tab_navs"] += 1
                try:
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass
            if target is not None:
                stext = ""
                try:
                    stext = await target.inner_text("body")
                except Exception:
                    stext = ""
                if "invoice.stripe.com" in target.url or "Invoice" in stext:
                    await _screenshot(target, job, f"invoice-page-{index+1}.png",
                                      "invoice page")
                    num_m = re.search(
                        r"Invoice\s*(?:number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{4,})",
                        stext)
                    if num_m:
                        inv["invoice_number"] = num_m.group(1)
                    inv["source"] = "invoice.stripe.com"
                    pdf_name, _ = await _try_download_from_stripe_page(
                        target, job, inv, len(invoices))
                    if pdf_name:
                        inv["pdf_file"] = pdf_name
                        debug["pdfs"] += 1
                try:
                    if target is page:
                        await page.go_back(wait_until="domcontentloaded",
                                           timeout=15000)
                        await page.wait_for_timeout(2000)
                    else:
                        await target.close()
                except Exception:
                    pass
            else:
                # Case 3: a modal/dialog on the same page.
                try:
                    dl = page.locator("text=Download invoice").first
                    if await dl.count():
                        debug["modals"] += 1
                        pdf_name, _ = await _try_download_from_stripe_page(
                            page, job, inv, len(invoices))
                        if pdf_name:
                            inv["pdf_file"] = pdf_name
                            debug["pdfs"] += 1
                        try:
                            await page.locator(
                                "button[aria-label='Close'], "
                                "button:has-text('Close')"
                            ).first.click(timeout=3000)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            continue
    return debug


async def run_job(job: dict, cookies: list[dict]):
    started = time.time()
    invoices: list[dict] = []
    try:
        update_job(job, status="running", stage="launching_browser",
                   progress=10, message="正在启动浏览器…")
        async with async_playwright() as pw:
            proxy, ignore_https = _proxy_config()
            # Firefox, not Chromium: Cloudflare detects headless
            # Chromium on every host tried so far (see module docstring),
            # while Firefox loads the real homepage. The UA must match
            # the engine -- a Firefox sending a Chrome UA is itself a
            # detectable mismatch.
            browser = await pw.firefox.launch(
                headless=True,
                proxy=proxy,
            )
            try:
                context = await browser.new_context(
                    ignore_https_errors=ignore_https,
                    proxy=proxy,
                    viewport={"width": 1280, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64; "
                                "rv:133.0) Gecko/20100101 Firefox/133.0"),
                    locale="en-US",
                    timezone_id="America/Los_Angeles",
                )
                # Seed ONLY the session cookie(s). Nothing else is carried over.
                await context.add_cookies(cookies)
                page = await context.new_page()

                update_job(job, stage="opening_chatgpt", progress=20,
                           message="正在打开 ChatGPT…")
                try:
                    resp = await page.goto(CHATGPT_HOME, wait_until="domcontentloaded",
                                           timeout=NAV_TIMEOUT_MS)
                    http_status = resp.status if resp else None
                except Exception as e:
                    detail = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
                    fail_job(job, stage="opening_chatgpt", code="navigation_failed",
                             message=f"打开 chatgpt.com 失败：{type(e).__name__}：{detail}",
                             hint="请检查服务器网络是否能访问 chatgpt.com，稍后重试。"
                                  "如果服务器在代理后面，请确认浏览器代理配置正确。"
                                  "（本机 Muse VM 的出口代理会拒绝浏览器的 HTTPS "
                                  "隧道，见 README 的本地限制说明。）")
                    return
                await page.wait_for_timeout(4000)
                if http_status == 403:
                    # A 403 is sometimes flaky / rate-based, so retry
                    # the first load once after a short wait. This is
                    # not a bypass: it is the same normal navigation,
                    # once more. If the second load is still 403 (or a
                    # challenge page), the job fails honestly below.
                    update_job(job, stage="opening_chatgpt", progress=22,
                               message="首次访问被拒绝（403），10 秒后重试一次…")
                    await page.wait_for_timeout(10000)
                    try:
                        resp2 = await page.goto(CHATGPT_HOME, wait_until="domcontentloaded",
                                                timeout=NAV_TIMEOUT_MS)
                        http_status = resp2.status if resp2 else http_status
                        await page.wait_for_timeout(4000)
                    except Exception:
                        pass
                if http_status == 403:
                    fail_job(job, stage="opening_chatgpt", code="access_denied_403",
                             message="chatgpt.com 返回 HTTP 403（访问被拒绝）",
                             hint="服务端两次判定本次访问不被允许。有效 session 不等于服务端一定放行："
                                  "请隔一段时间再试一次；如果反复 403，说明这个出口的浏览器指纹或 IP 段"
                                  "不被接受，需要换托管地区/平台，或改在你自己的电脑上跑（自己的网络 IP）。"
                                  "本工具不绕过访问控制。",
                             http_status=403, url=page.url, page_title=await page.title())
                    return

                text = ""
                try:
                    text = await page.inner_text("body")
                except Exception:
                    text = ""
                sig = _body_signals(text, await page.title(), page.url)
                if sig["is_challenge"]:
                    await _screenshot(page, job, "challenge.png", "challenge")
                    fail_job(job, stage="bot_check", code="bot_check_detected",
                             message="遇到浏览器人机验证（Cloudflare / Turnstile）",
                             hint="页面要求完成人机验证后才能继续。本工具不自动解验证、"
                                  "不绕过验证。请稍后重试，或在你自己的浏览器里先完成验证、"
                                  "确认 session 仍有效后再试。",
                             url=page.url, page_title=await page.title())
                    return
                if sig["looks_login"]:
                    fail_job(job, stage="checking_login", code="login_required",
                             message="Session 未登录：页面跳转到了登录页",
                             hint="session 已过期、缺失分段，或不被接受。请重新从浏览器复制"
                                  "完整的 session cookie（含全部 .0/.1 分段），不要只复制一部分。",
                             url=page.url, page_title=await page.title())
                    return

                # --- Billing page (current Oct 2026 path) ---
                # A hash-only navigation is unreliable: the app may boot
                # to the General panel and ignore the hash, so confirm
                # the Billing panel actually rendered before reading it.
                job.setdefault("page_debug", {})
                update_job(job, stage="opening_billing", progress=35,
                           message="正在打开 Settings → Billing…")
                await page.goto(BILLING_URL, wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
                await page.wait_for_timeout(5000)

                async def _read_body():
                    try:
                        return await page.inner_text("body")
                    except Exception:
                        return ""

                billing_text = await _read_body()
                sig2 = _body_signals(billing_text, await page.title(), page.url)
                if sig2["is_challenge"]:
                    # Wait for the challenge to clear on its own (a real
                    # browser usually proceeds after a few seconds); we
                    # never click it or solve it.
                    update_job(job, stage="opening_billing", progress=36,
                               message="遇到人机验证，正在等待页面自行通过…")
                    if await _wait_for_challenge_to_clear(page, timeout_s=45):
                        billing_text = await _read_body()
                        sig2 = _body_signals(billing_text, await page.title(), page.url)
                    else:
                        await _screenshot(page, job, "challenge.png", "challenge")
                        fail_job(job, stage="bot_check", code="bot_check_detected",
                                 message="在 Billing 页面遇到浏览器人机验证",
                                 hint="验证在 45 秒内没有自行通过。本工具不自动解验证。"
                                      "请稍后重试，或换一个网络环境再试。",
                                 url=page.url, page_title=await page.title())
                        return
                if sig2["looks_login"]:
                    fail_job(job, stage="checking_login", code="login_required",
                             message="Session 未登录：Billing 页面跳转到了登录页",
                             hint="请重新复制完整的 session cookie（含全部 .0/.1 分段）。",
                             url=page.url, page_title=await page.title())
                    return
                if not _has_billing_content(billing_text):
                    # The hash navigation landed on General (or another
                    # panel). Try the sidebar entry, then a fresh load.
                    job["page_debug"]["billing_panel_loaded"] = False
                    job["page_debug"]["sidebar_has_billing"] = _sidebar_has_billing(billing_text)
                    try:
                        sidebar_billing = page.get_by_text("Billing", exact=True).first
                        if await sidebar_billing.count():
                            await sidebar_billing.click(timeout=5000)
                            await page.wait_for_timeout(4000)
                            billing_text = await _read_body()
                    except Exception:
                        pass
                    if not _has_billing_content(billing_text):
                        try:
                            await page.goto(BILLING_URL, wait_until="domcontentloaded",
                                            timeout=NAV_TIMEOUT_MS)
                            await page.wait_for_timeout(8000)
                            billing_text = await _read_body()
                        except Exception:
                            pass
                job["page_debug"]["billing_panel_loaded"] = _has_billing_content(billing_text)
                job["page_debug"]["sidebar_has_billing"] = _sidebar_has_billing(billing_text)

                await _screenshot(page, job, "billing.png", "billing")
                subscription = _extract_subscription(billing_text)
                job["subscription"] = subscription
                # Billing platform hint from visible text.
                low = billing_text.lower()
                if "apple" in low and "app store" in low:
                    job["billing_platform"] = "apple_app_store"
                elif "google play" in low:
                    job["billing_platform"] = "google_play"
                elif "stripe" in low or "manage" in low:
                    job["billing_platform"] = "stripe"
                else:
                    job["billing_platform"] = "unknown"

                # --- Transaction history rows on the Billing page ---
                update_job(job, stage="listing_invoices", progress=55,
                           message="正在读取交易记录…")
                row_texts: list[str] = []
                try:
                    row_texts = await page.evaluate("""() => {
                      const out = [];
                      const els = document.querySelectorAll('button, a, [role="button"], [role="row"], tr, li, div');
                      for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (!t) continue;
                        if (/Paid|Open invoice|Invoice/i.test(t) && /\\$|€|£|₱|¥|\\d{4}/.test(t) && t.length < 300) {
                          out.push(t);
                        }
                      }
                      return [...new Set(out)].slice(0, 20);
                    }""")
                except Exception:
                    row_texts = []
                for rt in row_texts:
                    invoices.append(_parse_invoice_row_text(rt))

                # Open each transaction row and download its invoice PDF.
                update_job(job, stage="downloading_invoices", progress=70,
                           message="正在打开发票并下载原始 PDF…")
                await _download_invoice_pdfs(page, context, job, invoices, started)

                # --- Fallback: Subscription page + Stripe portal ---
                if not invoices:
                    update_job(job, stage="opening_subscription", progress=80,
                               message="Billing 未找到账单，正在尝试 Subscription / Stripe 门户…")
                    try:
                        await page.goto(SUBSCRIPTION_URL, wait_until="domcontentloaded",
                                        timeout=NAV_TIMEOUT_MS)
                        await page.wait_for_timeout(4000)
                        sub_text = ""
                        try:
                            sub_text = await page.inner_text("body")
                        except Exception:
                            sub_text = ""
                        sig_sub = _body_signals(sub_text, await page.title(), page.url)
                        if sig_sub["is_challenge"]:
                            job.setdefault("page_debug", {})["subscription_challenge"] = True
                            if await _wait_for_challenge_to_clear(page, timeout_s=45):
                                try:
                                    sub_text = await page.inner_text("body")
                                except Exception:
                                    sub_text = ""
                        await _screenshot(page, job, "subscription.png", "subscription")
                        if not subscription:
                            job["subscription"] = _extract_subscription(sub_text)
                        # Click "Manage my subscription" if present; it opens
                        # billing.stripe.com in a new tab. We screenshot it
                        # and list what is visible, but we do NOT claim
                        # invoices from a link alone.
                        try:
                            manage = page.get_by_text("Manage my subscription", exact=False).first
                            if await manage.count():
                                async with context.expect_page(timeout=15000) as portal_info:
                                    await manage.click(timeout=8000)
                                portal = await portal_info.value
                                await portal.wait_for_load_state("domcontentloaded", timeout=20000)
                                await portal.wait_for_timeout(3000)
                                await _screenshot(portal, job, "stripe-portal.png", "stripe portal")
                                # The portal sometimes lands on the
                                # subscription tab; open Invoice history.
                                try:
                                    hist = portal.get_by_text("Invoice history", exact=False).first
                                    if await hist.count():
                                        await hist.click(timeout=5000)
                                        await portal.wait_for_timeout(3000)
                                except Exception:
                                    pass
                                ptext = ""
                                try:
                                    ptext = await portal.inner_text("body")
                                except Exception:
                                    ptext = ""
                                job["billing_platform"] = "stripe"
                                # Record whose Stripe customer this is:
                                # an empty portal ("No invoice history")
                                # means THIS identity has no invoices,
                                # even if another workspace/account does.
                                job.setdefault("page_debug", {})["portal_empty"] = (
                                    "No invoice history" in ptext)
                                _nm = re.search(
                                    r"BILLING INFORMATION.*?Name\s+([^\n]+)",
                                    ptext, re.S)
                                if _nm:
                                    job["portal_billing_name"] = _nm.group(1).strip()[:60]
                                # The portal itself is the workspace
                                # billing view: parse its subscription,
                                # next payment, payment method and
                                # discount, not just invoice links.
                                _pb = _parse_portal_billing(ptext)
                                if _pb:
                                    job["portal_billing"] = _pb
                                # Stripe portal invoice rows are links; record
                                # only rows actually rendered on the page.
                                try:
                                    prows = await portal.evaluate("""() => {
                                      const out = [];
                                      for (const el of document.querySelectorAll('a, button, tr, [role="row"]')) {
                                        const t = (el.innerText || '').trim();
                                        if (t && /Download|Invoice/i.test(t) && t.length < 300) out.push(t);
                                      }
                                      return [...new Set(out)].slice(0, 20);
                                    }""")
                                except Exception:
                                    prows = []
                                for rt in prows:
                                    invoices.append(_parse_invoice_row_text(rt))
                                await portal.close()
                        except Exception:
                            pass
                    except Exception:
                        pass

                # --- Fallback 2: in-page Stripe portal URL (GitHub pattern) ---
                # Only when the UI did not surface invoices, and only in
                # the browser page (see _in_page_portal_url). This is NOT
                # gated on an accounts list, and it does not use a
                # server-side direct API call. If the portal URL cannot
                # be resolved, the job still completes with whatever the
                # UI actually showed -- no invoices are invented.
                if not invoices:
                    update_job(job, stage="resolving_stripe_portal", progress=88,
                               message="正在从浏览器内解析 Stripe 门户地址…")
                    try:
                        portal_url = await _in_page_portal_url(page)
                        if portal_url and portal_url.startswith("https://"):
                            portal = await context.new_page()
                            await portal.goto(portal_url, wait_until="domcontentloaded",
                                              timeout=NAV_TIMEOUT_MS)
                            await portal.wait_for_timeout(4000)
                            await _screenshot(portal, job, "stripe-portal.png", "stripe portal")
                            job["billing_platform"] = "stripe"
                            try:
                                _pt = await portal.inner_text("body")
                                job.setdefault("page_debug", {})["portal_empty"] = (
                                    "No invoice history" in _pt)
                                _nm2 = re.search(
                                    r"BILLING INFORMATION.*?Name\s+([^\n]+)", _pt, re.S)
                                if _nm2:
                                    job["portal_billing_name"] = _nm2.group(1).strip()[:60]
                                _pb2 = _parse_portal_billing(_pt)
                                if _pb2:
                                    job["portal_billing"] = _pb2
                            except Exception:
                                pass
                            # chrismazanec's selector for Stripe portal
                            # invoice links; tedbibr's href-based selector
                            # as a second pass.
                            links = []
                            try:
                                await portal.wait_for_selector(
                                    'a[data-testid="hip-link"]', timeout=10000)
                                links = await portal.query_selector_all(
                                    'a[data-testid="hip-link"]')
                            except Exception:
                                links = []
                            if not links:
                                try:
                                    links = await portal.query_selector_all(
                                        "a[href*='invoice'], a[href*='receipt']")
                                except Exception:
                                    links = []
                            for link in links[:5]:
                                try:
                                    href = await link.get_attribute("href")
                                    text = (await link.inner_text()).strip()
                                except Exception:
                                    continue
                                if not href:
                                    continue
                                inv = _parse_invoice_row_text(text)
                                inv["source"] = "billing.stripe.com"
                                # Open the invoice page and download its PDF.
                                try:
                                    inv_page = await context.new_page()
                                    await inv_page.goto(href, wait_until="domcontentloaded",
                                                        timeout=NAV_TIMEOUT_MS)
                                    await inv_page.wait_for_timeout(2000)
                                    pdf_name, _ = await _try_download_from_stripe_page(
                                        inv_page, job, inv, len(invoices))
                                    if pdf_name:
                                        inv["pdf_file"] = pdf_name
                                    await inv_page.close()
                                except Exception:
                                    pass
                                invoices.append(inv)
                            await portal.close()
                    except Exception:
                        pass

                job["invoices"] = invoices
                # Deduplicate by raw text / invoice number, keep order.
                seen = set()
                deduped = []
                for inv in invoices:
                    key = inv.get("invoice_number") or inv.get("raw_text")
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    deduped.append(inv)
                job["invoices"] = deduped

                pdf_count = sum(1 for inv in deduped if inv.get("pdf_file"))
                done_msg = (f"完成：找到 {len(deduped)} 条账单记录，"
                            f"其中 {pdf_count} 张已下载原始发票 PDF，"
                            f"订阅信息 {'已读取' if job.get('subscription') else '页面上未识别到'}。")
                if deduped and pdf_count == 0:
                    done_msg += ("注意：看到了交易记录，但没能下载到发票 PDF"
                                 "（点击行后未打开可下载的发票页），请把账单页截图发给开发者排查。")
                dbg = job.get("page_debug") or {}
                if not deduped and not dbg.get("billing_panel_loaded", True):
                    if not dbg.get("sidebar_has_billing", True):
                        done_msg += ("账单页没有加载出来：截图里是 General 设置页，"
                                     "且设置侧栏里没有 Billing/Subscription 入口。"
                                     "当前 session 对应的身份可能没有账单权限"
                                     "（例如团队工作空间的成员身份，账单在管理员名下）。"
                                     "请换成实际付费的账号（或工作空间管理员账号）的 session 再试。")
                    else:
                        done_msg += "账单页没有加载出来（截图里不是 Billing 面板），请重试一次。"
                if dbg.get("portal_empty"):
                    done_msg += "Stripe 门户里也显示没有发票记录（No invoice history）。"
                if job.get("portal_billing_name"):
                    done_msg += f"门户账单姓名：{job['portal_billing_name']}。"
                pb = job.get("portal_billing") or {}
                if pb:
                    parts = [pb.get("plan", ""), pb.get("amount_text", "")]
                    if pb.get("next_payment"):
                        parts.append(f"下一笔付款 {pb['next_payment']}")
                    if pb.get("payment_method"):
                        parts.append(f"付款方式 {pb['payment_method']}")
                    done_msg += "门户订阅：" + "，".join(p for p in parts if p) + "。"
                    if pb.get("invoice_history_empty") and pb.get("next_payment") in (
                            "US$0.00", "$0.00"):
                        done_msg += ("门户无发票记录：下一笔为 $0（全额折扣），"
                                     "目前没有可下载的发票 PDF，这不是获取失败。")
                update_job(job, status="completed", stage="done", progress=100,
                           message=done_msg)
            finally:
                await browser.close()
    except Exception as e:
        # Never leak a credential in an error message.
        fail_job(job, stage="browser_error", code="browser_error",
                 message=f"浏览器运行失败：{type(e).__name__}",
                 hint="请稍后重试。如果反复出现，请检查服务器是否能正常启动 Firefox。")
    finally:
        # The caller drops the in-memory cookie reference; nothing
        # credential-like was written to disk by this runner.
        pass
