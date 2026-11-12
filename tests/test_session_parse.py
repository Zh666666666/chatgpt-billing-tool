import sys, unittest
sys.path.insert(0, "app")
sys.path.insert(0, ".")
from app.session_parse import parse_session_cookie, parse_session_json, CredentialError

LONG = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0." + "a" * 200

class TestSessionParse(unittest.TestCase):
    def test_bare_value(self):
        cs = parse_session_cookie(LONG)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["name"], "__Secure-next-auth.session-token")
        self.assertEqual(cs[0]["value"], LONG)

    def test_chunked_not_joined(self):
        cs = parse_session_cookie(
            "__Secure-next-auth.session-token.0=" + "a" * 100 +
            "; __Secure-next-auth.session-token.1=" + "b" * 100)
        self.assertEqual([c["name"] for c in cs],
                         ["__Secure-next-auth.session-token.0",
                          "__Secure-next-auth.session-token.1"])
        # Chunks must be sent under their own names, never concatenated.
        self.assertNotIn("a" * 100 + "b" * 100, [c["value"] for c in cs])

    def test_chunked_drops_stale_plain(self):
        cs = parse_session_cookie(
            "__Secure-next-auth.session-token=stalevalue1234567890; "
            "__Secure-next-auth.session-token.0=" + "a" * 100)
        self.assertEqual(len(cs), 1)
        self.assertTrue(cs[0]["name"].endswith(".0"))

    def test_missing_chunk_raises(self):
        with self.assertRaises(CredentialError):
            parse_session_cookie(
                "__Secure-next-auth.session-token.0=" + "a" * 100 +
                "; __Secure-next-auth.session-token.2=" + "c" * 100)

    def test_full_cookie_header(self):
        cs = parse_session_cookie(
            "Cookie: __Secure-next-auth.session-token.0=" + "a" * 100 +
            "; cf_clearance=xyz123")
        names = [c["name"] for c in cs]
        self.assertIn("__Secure-next-auth.session-token.0", names)
        self.assertIn("cf_clearance", names)

    def test_no_session_raises(self):
        with self.assertRaises(CredentialError):
            parse_session_cookie("foo=bar; baz=qux")

    def test_json_cookies(self):
        import json
        raw = json.dumps({"cookies": [
            {"name": "__Secure-next-auth.session-token.0", "value": "a" * 100},
            {"name": "__Secure-next-auth.session-token.1", "value": "b" * 100},
        ]})
        cs = parse_session_json(raw)
        self.assertEqual(len(cs), 2)

    def test_short_value_raises(self):
        with self.assertRaises(CredentialError):
            parse_session_cookie("short")

    def test_auth_session_json_session_token(self):
        # The /api/auth/session JSON: WARNING_BANNER + sessionToken.
        # Only sessionToken is used; accessToken must NOT be accepted
        # as a session cookie.
        import json
        raw = json.dumps({
            "WARNING_BANNER": "DO NOT SHARE",
            "user": {"id": "user-123", "email": "a@example.com"},
            "accessToken": "eyJhbGciOiJSUzI1NiJ9.fake-access-token",
            "sessionToken": LONG,
            "expires": "2026-12-06T12:08:45.000Z",
        })
        cs = parse_session_json(raw)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["name"], "__Secure-next-auth.session-token")
        self.assertEqual(cs[0]["value"], LONG)

    def test_auth_session_json_access_token_only_raises(self):
        import json
        raw = json.dumps({"accessToken": "eyJhbGciOiJSUzI1NiJ9.fake"})
        with self.assertRaises(CredentialError):
            parse_session_json(raw)

    def test_whole_session_paste_over_20000_chars(self):
        # Regression for the user's report: pasting the whole session
        # export exceeds the old 20000-char server limit. A large JSON
        # (sessionToken + 25 KB of unrelated fields) must still parse
        # to just the session cookie.
        import json
        raw = json.dumps({
            "WARNING_BANNER": "DO NOT SHARE",
            "accessToken": "eyJhbGciOiJSUzI1NiJ9." + "x" * 2000,
            "sessionToken": LONG,
            "user": {"id": "user-123", "note": "y" * 25000},
        })
        self.assertGreater(len(raw), 20000)
        cs = parse_session_json(raw)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["value"], LONG)

    def test_whole_cookie_header_with_junk(self):
        # A full Cookie header with many unrelated cookies: only the
        # session cookie (and its chunks) may be kept.
        raw = ("oai-did=abc123; __Secure-next-auth.session-token=" + LONG +
               "; _cf_bm=zzz; oai-next-auth=qqq")
        cs = parse_session_cookie(raw)
        names = {c["name"] for c in cs}
        self.assertIn("__Secure-next-auth.session-token", names)
        self.assertNotIn("oai-did", names)
        self.assertNotIn("_cf_bm", names)

if __name__ == "__main__":
    unittest.main()
