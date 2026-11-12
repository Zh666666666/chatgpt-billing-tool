# Playwright official Python image already includes Chromium + system deps.
# This is required: a plain python:3.12 image cannot run the browser.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV MAX_CONCURRENT_JOBS=2
ENV JOB_TTL_SECONDS=1800
# The container runs as root in most PaaS Docker runtimes; Playwright
# needs --no-sandbox, which browser_runner.py already passes.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
