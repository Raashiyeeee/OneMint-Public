"""
Retry utilities with exponential backoff and jitter.

Configuration
-------------
RetryConfig controls:
  - max_attempts   Maximum number of total attempts (initial + retries)
  - base_delay_s   Initial back-off delay in seconds
  - max_delay_s    Cap on any single wait
  - jitter_factor  Fraction of the delay to randomise (0 = no jitter)

Retryable HTTP errors
---------------------
  429, 500, 502, 503, 504 and connection/timeout errors.

Non-retryable
-------------
  401, 403 (auth errors), ValueError, TypeError, PermissionError.
  These bubble up immediately.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

import aiohttp

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
NON_RETRYABLE_EXCEPTIONS = (PermissionError, ValueError, TypeError)


@dataclass
class RetryConfig:
    max_attempts: int = 4
    base_delay_s: float = 2.0
    max_delay_s: float = 30.0
    jitter_factor: float = 0.25


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    config: RetryConfig,
) -> T:
    """
    Execute ``coro_fn`` with exponential-backoff retry.

    Parameters
    ----------
    coro_fn:
        Zero-argument async callable that performs one attempt.
    config:
        Retry parameters.

    Returns
    -------
    T
        Whatever ``coro_fn`` returns on success.

    Raises
    ------
    Exception
        The last exception if all attempts are exhausted.
    """
    last_exc: Exception = RuntimeError("No attempts made")

    for attempt in range(1, config.max_attempts + 1):
        try:
            result = await coro_fn()
            if attempt > 1:
                log.info("[API_RETRY] Succeeded on attempt %d", attempt)
            return result

        except NON_RETRYABLE_EXCEPTIONS as exc:
            raise  # Immediately re-raise — not transient

        except aiohttp.ClientResponseError as exc:
            last_exc = exc
            if exc.status not in RETRYABLE_HTTP_STATUSES:
                raise  # e.g. 404 — not transient

            if attempt == config.max_attempts:
                log.error(
                    "[API_ERROR] All %d attempts exhausted — last status=%d",
                    config.max_attempts,
                    exc.status,
                )
                raise

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt == config.max_attempts:
                log.error(
                    "[API_ERROR] All %d attempts exhausted — last error=%s",
                    config.max_attempts,
                    exc,
                )
                raise

        # Compute next wait with jitter
        raw_delay = min(config.base_delay_s * (2 ** (attempt - 1)), config.max_delay_s)
        jitter = raw_delay * config.jitter_factor * (random.random() * 2 - 1)
        delay = max(0.1, raw_delay + jitter)

        log.warning(
            "[API_RETRY] attempt=%d/%d sleeping=%.1fs error=%s",
            attempt,
            config.max_attempts,
            delay,
            last_exc,
        )
        await asyncio.sleep(delay)

    raise last_exc
