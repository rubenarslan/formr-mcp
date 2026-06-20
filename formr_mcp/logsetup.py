"""Logging setup for formr-mcp.

A stdio MCP server must never write to stdout (that's the JSON-RPC channel),
so all logs go to stderr — where the MCP client (Claude Desktop / Code)
captures them. Level is controlled by FORMR_MCP_LOG_LEVEL (default INFO).

`install_tool_logging` wraps the FastMCP tool manager's `call_tool` so every
tool invocation logs name, a redacted/truncated arg preview, duration, and
success/error — centrally, with no per-tool decoration and no risk to
FastMCP's signature introspection.
"""
from __future__ import annotations

import logging
import os
import sys
import time

_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_SENSITIVE = ("secret", "token", "password")


def _level_from_env() -> int:
    raw = (os.getenv("FORMR_MCP_LOG_LEVEL") or "INFO").strip().upper()
    if raw not in _LEVELS:
        raw = "INFO"
    return getattr(logging, raw)


def setup_logging() -> None:
    """Configure root logging to stderr at FORMR_MCP_LOG_LEVEL (default INFO)."""
    level = _level_from_env()
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # httpcore is extremely chatty at DEBUG; pin it down. Show raw httpx request
    # lines only when explicitly debugging — our client logs requests at INFO.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(level if level <= logging.DEBUG else logging.WARNING)


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _preview(arguments) -> str:
    """Compact, truncated, secret-redacted one-liner of a tool's arguments."""
    if not isinstance(arguments, dict):
        return _trunc(repr(arguments), 200)
    parts = []
    for k, v in arguments.items():
        if any(s in str(k).lower() for s in _SENSITIVE):
            sval = "***"
        else:
            sval = _trunc(repr(v), 60)
        parts.append(f"{k}={sval}")
    return _trunc(", ".join(parts), 200)


def install_tool_logging(mcp) -> None:
    """Wrap mcp's tool manager call_tool to log every invocation. Idempotent."""
    tm = getattr(mcp, "_tool_manager", None)
    if tm is None or getattr(tm, "_formr_logged", False):
        return
    orig = tm.call_tool
    log = logging.getLogger("formr_mcp.tools")

    async def logged_call_tool(name, arguments, *args, **kwargs):
        t0 = time.perf_counter()
        log.info("tool %s(%s)", name, _preview(arguments))
        try:
            result = await orig(name, arguments, *args, **kwargs)
        except Exception as e:
            log.warning(
                "tool %s FAILED in %.0fms: %s: %s",
                name, (time.perf_counter() - t0) * 1000, type(e).__name__, e,
            )
            raise
        log.info("tool %s ok in %.0fms", name, (time.perf_counter() - t0) * 1000)
        return result

    tm.call_tool = logged_call_tool
    tm._formr_logged = True
