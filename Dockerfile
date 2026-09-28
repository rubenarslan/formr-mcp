# formr-mcp served over streamable-HTTP with a bearer-token gate (http_server.py).
# Secrets are NOT baked in — the .env (FORMR_* creds + MCP_BEARER_TOKEN) is
# bind-mounted at runtime (see docker-compose-custom.yml).
FROM python:3.12-slim

# uv for reproducible, lockfile-pinned installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer (cached unless the lock changes). --no-install-project:
# the project has no build-system; we import server.py / formr_mcp by path
# (cwd=/app) rather than as an installed package, so only deps are needed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code
COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    FORMR_WORKSPACE_DIR=/app/.formr \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000

EXPOSE 8000

CMD ["python", "http_server.py"]
