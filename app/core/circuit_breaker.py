"""
Circuit Breaker Pattern Implementation

Protects against cascading failures when external APIs are unavailable.
Uses pybreaker library for state management (CLOSED -> OPEN -> HALF-OPEN).
"""

import logging

import pybreaker

logger = logging.getLogger(__name__)


class LoggingListener(pybreaker.CircuitBreakerListener):
    """Listener that logs Circuit Breaker state changes and events."""

    def state_change(
        self,
        cb: pybreaker.CircuitBreaker,
        old_state: pybreaker.CircuitBreakerState,
        new_state: pybreaker.CircuitBreakerState,
    ) -> None:
        logger.warning(f"Circuit Breaker '{cb.name}' state change: {old_state.name} -> {new_state.name}")

    def failure(self, cb: pybreaker.CircuitBreaker, exc: Exception) -> None:
        logger.error(f"Circuit Breaker '{cb.name}' recorded failure: {exc}")

    def success(self, cb: pybreaker.CircuitBreaker) -> None:
        logger.debug(f"Circuit Breaker '{cb.name}' recorded success")


# Shared logging listener
_logging_listener = LoggingListener()


# =============================================================================
# Pre-configured Circuit Breakers for external services
# =============================================================================

polymarket_breaker = pybreaker.CircuitBreaker(
    name="polymarket_api",
    fail_max=5,  # Open after 5 consecutive failures
    reset_timeout=30,  # Try to recover after 30 seconds
    listeners=[_logging_listener],
)

firecrawl_breaker = pybreaker.CircuitBreaker(
    name="firecrawl_api",
    fail_max=5,
    reset_timeout=30,
    listeners=[_logging_listener],
)

openai_breaker = pybreaker.CircuitBreaker(
    name="openai_api",
    fail_max=5,
    reset_timeout=30,
    listeners=[_logging_listener],
)

serper_breaker = pybreaker.CircuitBreaker(
    name="serper_api",
    fail_max=5,
    reset_timeout=30,
    listeners=[_logging_listener],
)
