"""Calls to other CogniKart services, with trace propagation and timing.

Every dependency call is logged with its own latency and outcome, separately
from the request that triggered it. That separation is what lets the platform
say "the gateway was slow *because* payments was slow" instead of just
"something was slow".
"""
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import Request

from . import chaos, context, events, logging as klog, tracing
from .config import settings


class DependencyError(Exception):
    def __init__(self, code: str, status: int, latency_ms: int, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.status = status
        self.latency_ms = latency_ms
        self.detail = detail


async def call(
    request: Request,
    role: str,
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    timeout_s: Optional[float] = None,
) -> Tuple[int, Any, int]:
    """Call a downstream service. Returns (status, parsed_body, latency_ms).

    Raises DependencyError on timeout, connection failure or 5xx so callers
    can decide whether to retry -- the retry decision belongs to the caller
    (orders retries payments; the gateway does not).
    """
    base = settings.downstream(role)
    if not base:
        raise DependencyError(events.ErrorCode.INTERNAL_ERROR, 500, 0,
                              "no URL configured for %s" % role)

    url = base.rstrip("/") + path
    trace_id = getattr(request.state, "trace_id", tracing.new_trace_id())
    span_id = tracing.new_span_id()
    request_id = getattr(request.state, "request_id", "")
    headers = tracing.outbound_headers(trace_id, span_id, request_id)
    for h in ("x-session-id", "x-user-hash"):
        if h in request.headers:
            headers[h.title()] = request.headers[h]

    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout_s or settings.dependency_timeout_s) as client:
            resp = await client.request(method, url, json=json_body, headers=headers)
        latency_ms = int((time.time() - started) * 1000)
    except httpx.TimeoutException:
        latency_ms = int((time.time() - started) * 1000)
        _log_dep(trace_id, span_id, role, path, 504, latency_ms,
                 events.ErrorCode.UPSTREAM_TIMEOUT, "ERROR")
        raise DependencyError(events.ErrorCode.UPSTREAM_TIMEOUT, 504, latency_ms,
                              "%s timed out after %dms" % (role, latency_ms))
    except httpx.HTTPError as exc:
        latency_ms = int((time.time() - started) * 1000)
        _log_dep(trace_id, span_id, role, path, 503, latency_ms,
                 events.ErrorCode.UPSTREAM_UNAVAILABLE, "ERROR")
        raise DependencyError(events.ErrorCode.UPSTREAM_UNAVAILABLE, 503, latency_ms, str(exc))

    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}

    if resp.status_code >= 500:
        _log_dep(trace_id, span_id, role, path, resp.status_code, latency_ms,
                 events.ErrorCode.UPSTREAM_UNAVAILABLE, "ERROR")
        raise DependencyError(events.ErrorCode.UPSTREAM_UNAVAILABLE, resp.status_code,
                              latency_ms, str(body)[:200])

    _log_dep(trace_id, span_id, role, path, resp.status_code, latency_ms, None,
             "WARNING" if latency_ms > 1000 else "DEBUG")
    return resp.status_code, body, latency_ms


def _log_dep(trace_id: str, span_id: str, role: str, path: str, status: int,
             latency_ms: int, code: Optional[str], severity: str) -> None:
    klog.emit(
        severity,
        "dependency %s %s -> %d in %dms" % (role, path, status, latency_ms),
        events.HTTP_REQUEST_COMPLETED if status < 400 else events.HTTP_REQUEST_FAILED,
        trace=tracing.trace_field(trace_id),
        span_id=span_id,
        chaos_scenario=chaos.current_scenario(),
        dependency="cognikart-%s" % role,
        dependencyLatencyMs=latency_ms,
        route=path,
        httpStatus=status,
        errorCode=code,
    )
