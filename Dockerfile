# formr-mcp served over streamable-HTTP for every researcher of a formr
# instance (http_server.py): formr signs them in (OAuth), each request carries
# the caller's own formr token. The image holds no secret and needs none: set
# FORMR_BASE_URL and MCP_PUBLIC_URL (formr-docker's docker-compose-mcp.yml).
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
    MCP_HTTP_PORT=8000 \
    PYTHONDONTWRITEBYTECODE=1

# no root in the running container
RUN useradd --system --uid 10001 --no-create-home mcp
USER mcp

EXPOSE 8000

CMD ["python", "http_server.py"]
