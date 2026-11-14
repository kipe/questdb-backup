FROM python:3.14-slim-bookworm AS build

RUN pip install --no-cache-dir "uv==0.12.22"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_SYSTEM_PYTHON=1 \
    UV_PYTHON_DOWNLOADS=never

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim-bookworm

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

# The process runs as root so it can read a read-only QuestDB data mount.
# QuestDB files are often not world-readable.
ENTRYPOINT ["/app/.venv/bin/python", "-m", "questdb_backup"]

# 200 is healthy. 503 (last backup failed) makes this command fail.
# HEALTHCHECK_PORT=0 turns the server off, so the check passes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os,sys,urllib.request; port=os.environ.get('HEALTHCHECK_PORT','8080'); sys.exit(0) if port=='0' else urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=3)"]
