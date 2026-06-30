"""Logging configuration for cc-cred.

Set CC_CREDS_DEBUG=1 to enable full debug output with rich formatting.
Without it the logger is silent (WARNING+) so normal CLI use is unaffected.
"""

from __future__ import annotations

import json
import logging
import os
from json import JSONDecodeError
from typing import Any


_REDACTED_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
}


def configure_logging() -> None:
    """Call once at CLI entry point startup."""
    if os.environ.get("CC_CREDS_DEBUG", "0") == "1":
        from rich.console import Console
        from logspark import logger, spark_log_manager
        from logspark.Handlers.Rich.SparkRichHandler import SparkRichHandler

        # force_terminal=True ensures output even when stderr is a pipe
        # (common in PowerShell subprocesses where isatty() returns False).
        console = Console(stderr=True, force_terminal=True)
        logger.configure(level=logging.DEBUG, handler=SparkRichHandler(console=console))
        spark_log_manager.adopt_all()
        spark_log_manager.unify(copy_spark_logger_config=True, level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING)


def get_logger() -> logging.Logger:
    """Return the configured logger (logspark when debug on, stdlib otherwise)."""
    if os.environ.get("CC_CREDS_DEBUG", "0") == "1":
        from logspark import logger
        return logger
    return logging.getLogger("cc_cred")


def mask_token(token: str) -> str:
    """Show first 8 + last 4 chars of a token for safe log output."""
    if len(token) <= 14:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def _maybe_json(value: str) -> object:
    """Parse a string as JSON when possible; otherwise return the original string."""
    try:
        return json.loads(value)
    except JSONDecodeError:
        return value


def _redact_headers(headers: object) -> object:
    """Redact sensitive HTTP headers before logging."""
    if not isinstance(headers, dict):
        return headers

    return {
        key: "<redacted>" if str(key).lower() in _REDACTED_HEADERS else value
        for key, value in headers.items()
    }


def fmt(data: object, show_body: bool = True) -> str:
    """Format debug data as an indented JSON block.

    Usage:
        log.debug(f"response  status={code}" + fmt({"headers": ..., "body": ...}))

    Produces:
        response  status=200
        {
          "body": {...},
          "headers": {...}
        }

    JSON-looking string bodies are parsed when complete. Truncated/non-JSON
    bodies are left as strings so logging never crashes the caller.
    """
    if not isinstance(data, dict):
        return "\n" + json.dumps(data, indent=2, default=str)

    output: dict[str, Any] = {}

    if "body" in data:
        body = data["body"]
        if show_body and body:
            output["body"] = _maybe_json(body) if isinstance(body, str) else body

    for key, value in data.items():
        if key == "body":
            continue
        output[key] = _redact_headers(value) if key == "headers" else value

    return "\n" + json.dumps(output, indent=2, default=str)