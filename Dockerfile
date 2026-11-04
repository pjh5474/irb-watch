FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY irb_watch ./irb_watch
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "irb_watch.main:app", "--host", "0.0.0.0", "--port", "8000"]
