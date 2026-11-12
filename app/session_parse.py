"""
Session credential parsing.

Critical rule (verified against NextAuth behaviour, see README):
  __Secure-next-auth.session-token is chunked as
  __Secure-next-auth.session-token.0 / .1 / ... when > ~4KB.
  Chunks MUST be sent back under their own names, in index order,
  NEVER concatenated into one value. If both a plain cookie and
  chunks are present, the plain one is stale and must be dropped.

Accepted inputs for kind="session_cookie":
  - bare token value
  - "name=value" for the plain cookie or one chunk
  - several "name=value" pairs separated by ";" / newline
  - a full "Cookie:" header (with or without the "Cookie:" prefix)
  - JSON object / array exported from a browser (cookies array)

Accepted inputs for kind="session_json":
  - JSON with a "cookies" array, or a single cookie object,
    or {"__Secure-next-auth.session-token": "..."} etc.
  Only the session cookie (and optionally cf_clearance / __cf_bm
  if the user pasted a full header) is extracted; everything else
  is ignored.

Accepted inputs for kind="access_token":
  - a JWT / access token string. This is NOT interchangeable with
    a session cookie: it cannot log a browser into chatgpt.com,
    cannot open Settings / Billing, and cannot open the Stripe
    portal. The API layer records this limitation and the job
    fails fast with a clear capability error instead of pretending
    to run the browser flow.

No function here ever logs a credential value. Callers must only
log len(value) / chunk count.
"""
from __future__ import annotations

import json
import re

SESSION_BASE = "__Secure-next-auth.session-token"
# Other cookies we will keep ONLY if the user pasted a full Cookie header.
# They are optional, short-lived, and never required.
OPTIONAL_COOKIES = {"cf_clearance", "__cf_bm", "__Secure-next-auth.callback-url"}

class CredentialError(ValueError):
    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint

def _is_chunk_name(name: str) -> bool:
    return bool(re.fullmatch(re.escape(SESSION_BASE) + r"\.\d+", name))

def _chunk_index(name: str) -> int:
    return int(name.rsplit(".", 1)[1])

def _normalise_pairs(pairs: list[tuple[str, str]]) -> list[dict]:
    """pairs -> Playwright cookie dicts for chatgpt.com, chunk-aware."""
    by_name: dict[str, str] = {}
    for name, value in pairs:
        name = name.strip()
        value = value.strip().strip('"')
        if not name or not value:
            continue
        # Keep last occurrence; duplicates in a header are normally identical.
        by_name[name] = value

    chunks = sorted(
        (( _chunk_index(n), v) for n, v in by_name.items() if _is_chunk_name(n)),
        key=lambda x: x[0],
    )
    cookies: list[dict] = []
    if chunks:
        # Chunks present: use them exactly, drop stale plain cookie.
        indices = [i for i, _ in chunks]
        if indices != list(range(min(indices), max(indices) + 1)):
            raise CredentialError(
                "Session cookie 分段不连续",
                hint=f"已识别到分段 {indices}，中间缺失。请从浏览器 Cookie 中复制全部 "
                     f"{SESSION_BASE}.0、.1、… 分段，或直接复制整条 Cookie 请求头。",
            )
        if min(indices) != 0:
            raise CredentialError(
                "Session cookie 分段缺少 .0",
                hint="请复制全部 .0、.1、… 分段，不要只复制后面的分段。",
            )
        for idx, value in chunks:
            if len(value) < 20:
                raise CredentialError(
                    f"Session cookie 分段 .{idx} 过短，可能复制不完整",
                    hint="请重新复制该分段的完整值。",
                )
            cookies.append({
                "name": f"{SESSION_BASE}.{idx}",
                "value": value,
                "domain": ".chatgpt.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            })
    elif SESSION_BASE in by_name:
        value = by_name[SESSION_BASE]
        if len(value) < 20:
            raise CredentialError(
                "Session cookie 值过短，可能复制不完整",
                hint="请复制 __Secure-next-auth.session-token 的完整值，或复制整条 Cookie 请求头。",
            )
        cookies.append({
            "name": SESSION_BASE,
            "value": value,
            "domain": ".chatgpt.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        })
    else:
        raise CredentialError(
            "未找到 __Secure-next-auth.session-token",
            hint="请粘贴该 cookie 的完整值、分段 .0/.1/…，或整条 Cookie 请求头。"
                 "不要粘贴账号密码或 API Key。",
        )

    # Optional Cloudflare cookies, only if present in the paste.
    for name in ("cf_clearance", "__cf_bm"):
        if name in by_name:
            cookies.append({
                "name": name,
                "value": by_name[name],
                "domain": ".chatgpt.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            })
    return cookies

def _parse_cookie_header_text(text: str) -> list[tuple[str, str]]:
    text = text.strip()
    if not text:
        return []
    # Strip a leading "Cookie:" label if present.
    text = re.sub(r"(?i)^cookie\s*:\s*", "", text).strip()
    # Also strip a leading "Cookie" JSON-ish wrapper is handled elsewhere.
    pairs: list[tuple[str, str]] = []
    # Split on ";" or newline, but values are base64url/JWE without ";" so safe.
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        pairs.append((name.strip(), value.strip()))
    return pairs

def parse_session_cookie(raw: str) -> list[dict]:
    """Parse any session-cookie paste into Playwright cookies."""
    if raw is None or not str(raw).strip():
        raise CredentialError("Session 为空", hint="请粘贴 session cookie 的完整值。")
    text = str(raw).strip()
    # JSON paste? Try it first if it looks like JSON.
    if text.startswith("{") or text.startswith("["):
        try:
            return parse_session_json(text)
        except CredentialError:
            raise
        except Exception:
            # Fall through to header parsing; error below will explain.
            pass
    # Bare value (no "=" and no ";"): treat as the plain cookie value.
    if "=" not in text and ";" not in text and "\n" not in text:
        if len(text) < 20:
            raise CredentialError("Session 值过短，可能复制不完整",
                                  hint="请复制完整值，不要只复制一部分。")
        return _normalise_pairs([(SESSION_BASE, text)])
    pairs = _parse_cookie_header_text(text)
    if not pairs:
        raise CredentialError("无法解析粘贴的内容",
                              hint="请粘贴 cookie 值、name=value，或整条 Cookie 请求头。")
    return _normalise_pairs(pairs)

def parse_session_json(raw: str) -> list[dict]:
    """Parse a browser-exported Session JSON. Extract session cookie only."""
    try:
        data = json.loads(raw)
    except Exception:
        raise CredentialError("Session JSON 不是合法 JSON",
                              hint="请粘贴浏览器导出的完整 JSON，或改用 session cookie 方式。")
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        # Format A: the /api/auth/session JSON response. It starts with
        # a WARNING_BANNER and carries the session cookie value in the
        # top-level "sessionToken" field (NOT "accessToken" -- an access
        # token alone cannot log a browser in, see main.py).
        # Example keys: WARNING_BANNER, user, accessToken, sessionToken,
        # expires, authProvider. Only sessionToken is used here.
        if isinstance(data.get("sessionToken"), str) and data["sessionToken"].strip():
            pairs.append((SESSION_BASE, data["sessionToken"].strip()))
        cookies = data.get("cookies") or data.get("cookie") or []
        if isinstance(cookies, list):
            for c in cookies:
                if isinstance(c, dict) and c.get("name") and c.get("value"):
                    pairs.append((str(c["name"]), str(c["value"])))
        else:
            for k, v in data.items():
                if isinstance(v, str) and (k == SESSION_BASE or _is_chunk_name(k)):
                    pairs.append((k, v))
    elif isinstance(data, list):
        for c in data:
            if isinstance(c, dict) and c.get("name") and c.get("value"):
                pairs.append((str(c["name"]), str(c["value"])))
    if not pairs:
        raise CredentialError(
            "Session JSON 中没有找到 session cookie",
            hint="JSON 里需要包含 sessionToken 字段、__Secure-next-auth.session-token "
                 "或其 .0/.1 分段，或 cookies 数组。"
                 "注意：只含 accessToken 的 JSON 不能用，access token 不能替代 session cookie。",
        )
    return _normalise_pairs(pairs)

def describe_cookies(cookies: list[dict]) -> dict:
    """Safe, non-sensitive summary for logs / UI. Never includes values."""
    session = [c for c in cookies if c["name"].startswith(SESSION_BASE)]
    return {
        "session_cookie_chunks": len(session),
        "session_cookie_names": [c["name"] for c in session],
        "total_cookie_length": sum(len(c["value"]) for c in cookies),
        "has_cf_clearance": any(c["name"] == "cf_clearance" for c in cookies),
    }
