"""Logging configuration for cc-cred.

Set CC_CREDS_DEBUG=1 to enable full debug output with rich formatting.
Without it the logger is silent (WARNING+) so normal CLI use is unaffected.
"""
import logging
import os


def configure_logging() -> None:
    """Call once at CLI entry point startup."""
    from logspark import logger, spark_log_manager
    from logspark.Handlers.Rich.SparkRichHandler import SparkRichHandler

    if os.environ.get("CC_CREDS_DEBUG"):
        logger.configure(level=logging.DEBUG, handler=SparkRichHandler())
        spark_log_manager.adopt_all()
        spark_log_manager.unify(copy_spark_logger_config=True, level=logging.WARNING)
    else:
        logger.configure(level=logging.WARNING)


def get_logger() -> logging.Logger:
    """Return the configured logspark logger."""
    from logspark import logger
    return logger


def mask_token(token: str) -> str:
    """Show first 8 + last 4 chars of a token for safe log output."""
    if len(token) <= 14:
        return "***"
    return f"{token[:8]}...{token[-4:]}"
