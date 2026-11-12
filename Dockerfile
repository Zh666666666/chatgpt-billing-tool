# Playwright official Python image already includes Chromium + system deps.
# This is required: a plain python:3.12 image cannot run the browser.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The runner uses Firefox, not Chromium: Cloudflare detects headless
# Chromium on every host tried so far (Render Virginia/Singapore and a
# rented VPS all 403 on the first navigation), while Firefox loads the
# real homepage. Installed here so the image is self-contained. (Must
# run AFTER pip install: the playwright CLI comes from requirements.)
RUN python -m playwright install firefox --with-deps

COPY app ./app

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV MAX_CONCURRENT_JOBS=2
ENV JOB_TTL_SECONDS=1800
# The container runs as root in most PaaS Docker runtimes; Playwright
# needs --no-sandbox, which browser_runner.py already passes.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
