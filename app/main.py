"""
FastAPI entrypoint. Serves the mobile frontend and the job API.

Security notes (enforced here, not just documented):
- The session value is accepted only in a POST JSON body, never in
  a URL. It is never logged, never written to disk, and lives only
  in an in-memory dict keyed by job_id until the browser run ends.
- Files are served only with the per-job file_token, checked with
  hmac.compare_digest, and only for 30 minutes after job creation.
- No purchase / cancel / modify endpoint exists. The browser runner
  only navigates, screenshots, and downloads invoices.
- Access tokens are NOT interchangeable with a session cookie:
  POST with kind="access_token" is rejected with a capability
  error before any browser is launched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .browser_runner import run_job
from .session_parse import CredentialError, parse_session_cookie, parse_session_json
from .store import (check_file_token, delete_job, get_job, job_dir,
                    new_job, public_job, sweep_expired)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chatgpt-billing-tool")
# Never log request bodies / query strings (they could contain a token).
logging.getLogger("uvicorn.access").disabled = True

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
MAX_CREDENTIAL_CHARS = 20000

app = FastAPI(title="ChatGPT 订阅与账单查询", version="0.1.0")

# job_id -> Playwright cookies (in memory only, deleted after the run).
_pending_cookies: dict[str, list[dict]] = {}
_running = 0
_rate: dict[str, list[float]] = defaultdict(list)

@app.on_event("startup")
async def _startup():
    async def sweeper():
        while True:
            await asyncio.sleep(60)
            try:
                sweep_expired()
            except Exception:
                pass
    asyncio.create_task(sweeper())

@app.get("/api/health")
async def health():
    return {"ok": True, "running_jobs": _running,
            "max_concurrent_jobs": MAX_CONCURRENT_JOBS}

@app.post("/api/jobs")
async def create_job(req: Request):
    global _running
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    credential = body.get("credential") or ""
    kind = body.get("credential_kind") or "session_cookie"
    if kind not in ("session_cookie", "session_json", "access_token"):
        raise HTTPException(400, "credential_kind 不支持")
    if not isinstance(credential, str) or not credential.strip():
        raise HTTPException(400, "请先粘贴 session")
    if len(credential) > MAX_CREDENTIAL_CHARS:
        raise HTTPException(400, "粘贴的内容过长（超过 20000 字符）")

    # Rate limit: 10 jobs / IP / hour (best effort, in memory).
    ip = req.client.host if req.client else "unknown"
    now = time.time()
    _rate[ip] = [t for t in _rate[ip] if now - t < 3600]
    if len(_rate[ip]) >= 10:
        raise HTTPException(429, "请求过于频繁，请一小时后再试")
    _rate[ip].append(now)

    if kind == "access_token":
        # Fail fast, with the capability limit stated plainly.
        raise HTTPException(400, (
            "access token 不能替代 session cookie：它无法让浏览器登录 "
            "chatgpt.com，也打不开 Settings / Billing 和 Stripe 门户，"
            "所以无法截图、也无法下载发票 PDF。"
            "请粘贴 __Secure-next-auth.session-token（或其 .0/.1 分段）。"
        ))
    try:
        if kind == "session_json":
            cookies = parse_session_json(credential)
        else:
            cookies = parse_session_cookie(credential)
    except CredentialError as e:
        raise HTTPException(400, e.message + (f" {e.hint}" if e.hint else ""))

    if _running >= MAX_CONCURRENT_JOBS:
        raise HTTPException(429, "当前任务已满（同时最多 2 个），请稍等一分钟再试")

    job = new_job(kind)
    _pending_cookies[job["job_id"]] = cookies
    # Do not log cookie lengths in a way that could be confused with
    # values; log only the job id and chunk count.
    log.info("job %s created kind=%s chunks=%s",
             job["job_id"], kind,
             sum(1 for c in cookies if c["name"].startswith("__Secure-next-auth.session-token")))

    async def _run():
        global _running
        _running += 1
        try:
            cookies_local = _pending_cookies.pop(job["job_id"], [])
            await run_job(job, cookies_local)
        finally:
            _running -= 1
            _pending_cookies.pop(job["job_id"], None)

    asyncio.create_task(_run())
    return JSONResponse({
        "job_id": job["job_id"],
        "file_token": job["file_token"],
        "expires_at": job["expires_at"],
        "status": job["status"],
    })

@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在或已过期（文件保留 30 分钟）")
    return public_job(job)

@app.get("/api/jobs/{job_id}/files/{filename}")
async def get_file(job_id: str, filename: str, token: str = "", download: int = 0):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在或已过期")
    if not check_file_token(job, token):
        raise HTTPException(403, "文件凭证无效")
    safe = Path(filename).name
    if safe != filename or ".." in filename:
        raise HTTPException(400, "文件名无效")
    path = job_dir(job_id) / safe
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    entry = next((f for f in job["files"] if f["name"] == safe), None)
    content_type = entry["content_type"] if entry else "application/octet-stream"
    return FileResponse(
        path, media_type=content_type,
        filename=safe,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )

@app.delete("/api/jobs/{job_id}")
async def delete_job_endpoint(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在或已过期")
    _pending_cookies.pop(job_id, None)
    delete_job(job_id)
    return {"ok": True, "deleted": job_id}

# Static frontend last, so /api/* is not shadowed.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
