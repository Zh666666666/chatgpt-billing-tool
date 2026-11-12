"""Offline tests: no chatgpt.com access needed.

Proves the browser pipeline (Chromium launch, screenshot, DOM
extraction, invoice-row parsing, per-job file isolation) works,
even though this VM's egress proxy blocks Chromium's HTTPS tunnel
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
    def test_chromium_screenshot_and_isolation(self):
        async def run():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                b = await pw.chromium.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
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


if __name__ == "__main__":
    unittest.main()
