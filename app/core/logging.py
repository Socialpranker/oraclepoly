"""
Structured Logging with structlog

Provides JSON-formatted structured logging for production observability.
All log events include timestamp, log level, and contextual fields.
"""

import logging
import sys

import structlog


def setup_logging() -> None:
    """
    Configure structlog for JSON structured logging.

    Usage after calling setup_logging():
        import structlog
        logger = structlog.get_logger()
        logger.info("event_name", user_id=123, market_id="abc")
    """

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Redirect standard logging to structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )
