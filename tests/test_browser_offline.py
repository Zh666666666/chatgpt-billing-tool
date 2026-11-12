"""Offline tests: no chatgpt.com access needed.

Proves the browser pipeline (Firefox launch, screenshot, DOM
extraction, invoice-row parsing, per-job file isolation) works,
even though this VM's egress proxy blocks the browser's HTTPS tunnel
to chatgpt.com -- see README "本地限制". Real-account E2E still
needs a deployment with direct internet.
"""
import asyncio
import sys
import unittest

sys.path.insert(0, ".")
from app.browser_runner import _extract_subscription, _parse_invoice_row_text
from app.store import new_job, save_file, check_file_token, delete_job


class TestExtraction(unittest.TestCase):
    def test_subscription_fields(self):
        text = ("Settings Billing Current plan: ChatGPT Plus "
                "Next billing date: December 11, 2026 $20.00 / month Active")
        sub = _extract_subscription(text)
        self.assertIsNotNone(sub)
        self.assertIn("Plus", sub.get("plan", ""))
        self.assertEqual(sub.get("status"), "Active")

    def test_subscription_empty_returns_none(self):
        self.assertIsNone(_extract_subscription("hello world, no plan here"))
        self.assertIsNone(_extract_subscription(""))

    def test_invoice_row(self):
        inv = _parse_invoice_row_text("Nov 11, 2026  ChatGPT Plus  $20.00  Paid")
        self.assertEqual(inv["date"], "Nov 11, 2026")
        self.assertEqual(inv["amount_text"], "$20.00")
        self.assertEqual(inv["status"], "Paid")

    def test_invoice_row_stripe_date_shape(self):
        # Stripe portal shape from tedbibr's parser: "12 Feb 2026".
        inv = _parse_invoice_row_text("12 Feb 2026  US$20.00  Paid")
        self.assertEqual(inv["date"], "12 Feb 2026")
        self.assertEqual(inv["status"], "Paid")

    def test_pdf_bytes_validation(self):
        async def run():
            from app.browser_runner import _save_pdf_bytes
            from app.store import new_job, delete_job
            job = new_job("session_cookie")
            try:
                self.assertIsNone(await _save_pdf_bytes(job, b"<html>not a pdf</html>", "x.pdf", 0))
                name = await _save_pdf_bytes(job, b"%PDF-1.7 fake", "Invoice-123.pdf", 0)
                self.assertEqual(name, "Invoice-123.pdf")
                self.assertEqual(job["files"][0]["kind"], "invoice_pdf")
            finally:
                delete_job(job["job_id"])
        asyncio.run(run())


class TestBrowserOffline(unittest.TestCase):
    def test_firefox_screenshot_and_isolation(self):
        async def run():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                b = await pw.firefox.launch(headless=True)
                p = await b.new_page(viewport={"width": 1280, "height": 900})
                await p.set_content(
                    "<h1>Billing</h1><p>Current plan: ChatGPT Plus</p>"
                    "<p>Next billing date: December 11, 2026</p>")
                text = await p.inner_text("body")
                self.assertIn("Plus", _extract_subscription(text)["plan"])
                data = await p.screenshot(type="png")
                self.assertTrue(data.startswith(b"\x89PNG"))
                await b.close()
                return data
        data = asyncio.run(run())

        job_a = new_job("session_cookie")
        job_b = new_job("session_cookie")
        try:
            save_file(job_a, "billing.png", data, kind="screenshot",
                      content_type="image/png")
            # Token for job B must NOT open job A's file.
            self.assertFalse(check_file_token(job_a, job_b["file_token"]))
            self.assertTrue(check_file_token(job_a, job_a["file_token"]))
            self.assertEqual(job_a["files"][0]["name"], "billing.png")
            self.assertEqual(job_b["files"], [])
        finally:
            delete_job(job_a["job_id"])
            delete_job(job_b["job_id"])


class TestInvoiceRowClick(unittest.TestCase):
    def test_same_tab_invoice_download(self):
        """The Billing row carries no word 'Invoice'; clicking it must
        still find the invoice page (same-tab navigation here) and
        download the original PDF. The invoice page is served from a
        local HTTP server: Firefox blocks top-level navigation to
        data: URLs, so a data: URL cannot stand in for it."""
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, HTTPServer

        invoice_html = (
            "<h1>Invoice number INV-TEST-0001</h1>"
            "<p>ChatGPT Plus Subscription (per seat)</p>"
            "<button id=\"dl\">Download invoice</button>"
            "<script>document.getElementById('dl').onclick = () => {"
            "const blob = new Blob(['%PDF-1.7 fake-invoice'],"
            "{type: 'application/pdf'});"
            "const a = document.createElement('a');"
            "a.href = URL.createObjectURL(blob);"
            "a.download = 'Invoice-INV-TEST-0001.pdf'; a.click();};</script>"
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = invoice_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        billing_html = (
            "<h1>Billing</h1>"
            f"<a role=\"button\" href=\"http://127.0.0.1:{port}/invoice\">"
            "Nov 12, 2026  ChatGPT Plus  \u20b1982.14  Paid</a>"
        )

        async def run():
            from playwright.async_api import async_playwright
            from app.browser_runner import _download_invoice_pdfs
            async with async_playwright() as pw:
                b = await pw.firefox.launch(headless=True)
                ctx = await b.new_context()
                page = await ctx.new_page()
                await page.set_content(billing_html)
                job = new_job("session_cookie")
                try:
                    row_text = (await page.inner_text("a")).strip()
                    invoices = [_parse_invoice_row_text(row_text)]
                    debug = await _download_invoice_pdfs(
                        page, ctx, job, invoices, time.time())
                    self.assertEqual(debug["rows_clicked"], 1)
                    self.assertEqual(debug["same_tab_navs"], 1)
                    self.assertEqual(debug["pdfs"], 1)
                    self.assertEqual(invoices[0].get("invoice_number"),
                                     "INV-TEST-0001")
                    pdf_name = invoices[0].get("pdf_file")
                    self.assertIsNotNone(pdf_name)
                    saved = [f for f in job["files"]
                             if f["name"] == pdf_name]
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(saved[0]["kind"], "invoice_pdf")
                    return debug
                finally:
                    delete_job(job["job_id"])
                    await b.close()
        try:
            asyncio.run(run())
        finally:
            server.shutdown()


class TestPortalBillingParsing(unittest.TestCase):
    def test_business_portal_chinese(self):
        from app.browser_runner import _parse_portal_billing
        text = ("Manage your OpenAI billing settings 当前订阅 "
                "ChatGPT Business Subscription US$25.00 /每 1 个月 "
                "您的服务将于 2026年11月29日 更新。您的下一笔付款预计为 US$0.00 "
                "11月29日-2026年12月29日 ChatGPT Business Subscription US$25.00 "
                "小计 US$25.00 smb_channel_partner_talen_6ae0ca1f423c8 -US$25.00 "
                "2030年11月29日折扣结束 合计 US$0.00 "
                "Mastercard •••• 4675 INVOICE HISTORY No invoice history.")
        pb = _parse_portal_billing(text)
        self.assertIsNotNone(pb)
        self.assertEqual(pb["plan"], "ChatGPT Business Subscription")
        self.assertEqual(pb["next_payment"], "US$0.00")
        self.assertEqual(pb["payment_method"], "Mastercard •••• 4675")
        self.assertEqual(pb["renews"], "2026年11月29日")
        self.assertTrue(pb["invoice_history_empty"])
        self.assertIn("smb_channel_partner", pb.get("discount", ""))

    def test_portal_empty_english(self):
        from app.browser_runner import _parse_portal_billing
        pb = _parse_portal_billing("PAYMENT METHOD No payment method. "
            "BILLING INFORMATION Name jery INVOICE HISTORY No invoice history.")
        self.assertIsNotNone(pb)
        self.assertTrue(pb["invoice_history_empty"])
        self.assertNotIn("payment_method", pb)

    def test_portal_nothing_returns_none(self):
        from app.browser_runner import _parse_portal_billing
        self.assertIsNone(_parse_portal_billing("hello world"))
        self.assertIsNone(_parse_portal_billing(""))


class TestBillingPanelDetection(unittest.TestCase):
    def test_general_panel_is_not_billing(self):
        from app.browser_runner import _has_billing_content, _sidebar_has_billing
        general = ("Settings General Language Send shortcut Higher intelligence "
                   "Available reasoning efforts")
        self.assertFalse(_has_billing_content(general))
        billing = ("Settings Billing Current plan: ChatGPT Plus "
                   "Transaction history Nov 12, 2026 ChatGPT Plus Paid")
        self.assertTrue(_has_billing_content(billing))
        sidebar_no_billing = ("General Appearance Notifications Profile "
                              "Security and login Account Archived chats")
        self.assertFalse(_sidebar_has_billing(sidebar_no_billing))
        sidebar_with_billing = sidebar_no_billing + " Billing Subscription"
        self.assertTrue(_sidebar_has_billing(sidebar_with_billing))

    def test_wait_for_challenge_to_clear(self):
        async def run():
            from playwright.async_api import async_playwright
            from app.browser_runner import _wait_for_challenge_to_clear
            async with async_playwright() as pw:
                b = await pw.firefox.launch(headless=True)
                page = await b.new_page()
                # Challenge text that clears itself after ~1.5s.
                await page.set_content(
                    "<body>Verify you are human</body>"
                    "<script>setTimeout(() => { document.body.textContent = "
                    "'ChatGPT home'; }, 1500);</script>")
                self.assertTrue(await _wait_for_challenge_to_clear(
                    page, timeout_s=10))
                # Challenge that never clears.
                await page.set_content("<body>Verify you are human</body>")
                self.assertFalse(await _wait_for_challenge_to_clear(
                    page, timeout_s=4))
                await b.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
