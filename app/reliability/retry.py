"""Retry classification and backoff (FR-8.2, FR-8.5)."""
from __future__ import annotations
import asyncio, random
from typing import Any, Awaitable, Callable

import httpx

from app.contracts import AgentError

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404, 405, 409, 422}


class ToolFailure(Exception):
    def __init__(self, error: AgentError):
        super().__init__(error.message)
        self.error = error


def classify(exc: Exception, agent: str, default_code: str = "UNKNOWN_ERROR") -> AgentError:
    """Retryable = transient. Non-retryable = will fail identically next time (FR-8.2)."""
    if isinstance(exc, ToolFailure):
        return exc.error

    status = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    else:
        status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)

    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException, httpx.ConnectError)):
        return AgentError(error_code="UPSTREAM_TIMEOUT", message=str(exc), retryable=True, agent=agent)
    if status in RETRYABLE_STATUS:
        return AgentError(error_code=f"UPSTREAM_{status}", message=str(exc), retryable=True, agent=agent)
    if status in NON_RETRYABLE_STATUS:
        code = "INVALID_CREDENTIALS" if status in (401, 403) else f"CLIENT_ERROR_{status}"
        return AgentError(error_code=code, message=str(exc), retryable=False, agent=agent)
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return AgentError(error_code="MALFORMED_REQUEST", message=str(exc), retryable=False, agent=agent)
    return AgentError(error_code=default_code, message=str(exc), retryable=True, agent=agent)


async def with_retry(
    fn: Callable[[], Awaitable[Any]], *, agent: str, error_code: str = "UNKNOWN_ERROR",
    attempts: int = 3, base_delay: float = 1.0,
    trace_id: str | None = None, session_id: str | None = None, tool: str | None = None,
) -> Any:
    """3 attempts, 1s/2s/4s with jitter. Non-retryable errors are never retried."""
    from app.observability.logger import log_event

    last: AgentError | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await fn()
            if attempt > 1 and trace_id:
                await log_event("retry", trace_id=trace_id, session_id=session_id, agent=agent,
                                payload={"tool": tool, "outcome": f"succeeded_after_{attempt - 1}_retries"})
            return result
        except Exception as exc:  # noqa: BLE001 - deliberately broad, classified below
            last = classify(exc, agent, error_code)
            if trace_id:
                await log_event("retry", trace_id=trace_id, session_id=session_id, agent=agent,
                                payload={"tool": tool, "attempt": attempt, "error_code": last.error_code,
                                         "retryable": last.retryable, "message": last.message[:300]})
            if not last.retryable or attempt == attempts:
                break
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.3))

    if trace_id:
        await log_event("retry", trace_id=trace_id, session_id=session_id, agent=agent,
                        payload={"tool": tool,
                                 "outcome": "failed_gave_up" if (last and not last.retryable) else "failed_fell_back",
                                 "error_code": last.error_code if last else error_code})
    raise ToolFailure(last or AgentError(error_code=error_code, message="unknown", retryable=False, agent=agent))
