# IRB Watch API with headless Chromium (needed for the members-only login).
# Build:  docker compose up -d --build     (see docs/deploy-ec2.md)
FROM python:3.12-slim

# Pinned so a rebuild next month does not pick up a different uv.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first (cached until pyproject.toml / uv.lock change). --frozen installs exactly
# the versions in uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Chromium and the system libraries it needs; kept outside /app so it is shared by any user.
RUN /app/.venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY irb_watch ./irb_watch

# Do not run the web app as root.
RUN useradd --create-home --shell /usr/sbin/nologin app && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# One worker only: the daily scheduler runs inside this process, and a second worker
# would start a second scheduler and scan everything twice.
CMD ["/app/.venv/bin/uvicorn", "irb_watch.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
