import unittest
from unittest.mock import AsyncMock, patch
from app.browser_runner import _check_page, _fetch_workspace_billing
from app.store import new_job, delete_job, public_job


class ChallengeRegression(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.job = new_job('session_cookie')
        self.page = AsyncMock()
        self.page.url = 'https://chatgpt.com/admin'
        self.page.title.return_value = 'Just a moment...'
        self.page.inner_text.return_value = 'Verify you are human'

    def tearDown(self):
        delete_job(self.job['job_id'])

    async def test_blocked_workspace_stops_before_parsing(self):
        with patch('app.browser_runner._wait_for_challenge_to_clear', AsyncMock(return_value=False)), patch('app.browser_runner._screenshot', AsyncMock()) as shot, patch('app.browser_runner._parse_workspace_page', AsyncMock()) as parse:
            self.assertFalse(await _fetch_workspace_billing(self.page, None, self.job, [], 0))
            self.assertEqual(self.job['error']['code'], 'bot_check')
            self.assertEqual(self.job['status'], 'failed')
            self.page.goto.assert_awaited_once()
            parse.assert_not_awaited()
            self.assertGreaterEqual(shot.await_count, 2)
            self.assertEqual(public_job(self.job)['page_debug']['stages'][0]['outcome'], 'bot_check')

    async def test_clearance_reenters_target(self):
        self.page.inner_text.side_effect = ['Verify you are human', 'Workspace billing']
        self.page.title.side_effect = ['Just a moment...', 'Settings']
        with patch('app.browser_runner._wait_for_challenge_to_clear', AsyncMock(return_value=True)):
            self.assertTrue(await _check_page(self.page, self.job, 'billing', 'https://chatgpt.com/#settings/Billing'))
        self.page.goto.assert_awaited_once()
        self.assertEqual(self.job['page_debug']['stages'][0]['outcome'], 'ready')

    async def test_challenge_recurs_after_retry(self):
        with patch('app.browser_runner._wait_for_challenge_to_clear', AsyncMock(side_effect=[True, False])), patch('app.browser_runner._screenshot', AsyncMock()):
            self.assertFalse(await _check_page(self.page, self.job, 'billing', 'https://chatgpt.com/#settings/Billing'))
        self.assertEqual(self.job['error']['code'], 'bot_check')
        self.page.goto.assert_awaited_once()
