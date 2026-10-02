"""Per-request context, so every log line is trace-correlated automatically.

Without this, only the middleware's http.request.* lines carried a trace id
and the business events (checkout.started, payment.authorize.ok, ...) did not
-- which is backwards, because the business events are exactly what an
incident timeline is built from.

Threading the request object into every logging call would work but would
pollute every function signature. contextvars give us the same result with no
call-site changes: the middleware sets the context once per request and
klog.emit picks it up as a default.

contextvars propagate into Starlette's threadpool for sync `def` handlers
(anyio copies the context), so this works for both sync and async endpoints.
"""
from contextvars import ContextVar
from typing import Any, Dict, Optional

_trace_id: ContextVar[Optional[str]] = ContextVar("ck_trace_id", default=None)
_span_id: ContextVar[Optional[str]] = ContextVar("ck_span_id", default=None)
_request_id: ContextVar[Optional[str]] = ContextVar("ck_request_id", default=None)
_session_id: ContextVar[Optional[str]] = ContextVar("ck_session_id", default=None)
_user_hash: ContextVar[Optional[str]] = ContextVar("ck_user_hash", default=None)
_route: ContextVar[Optional[str]] = ContextVar("ck_route", default=None)


def set_request(
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_hash: Optional[str] = None,
    route: Optional[str] = None,
) -> None:
    _trace_id.set(trace_id)
    _span_id.set(span_id)
    _request_id.set(request_id)
    _session_id.set(session_id)
    _user_hash.set(user_hash)
    _route.set(route)


def trace_id() -> Optional[str]:
    return _trace_id.get()


def span_id() -> Optional[str]:
    return _span_id.get()


def request_id() -> Optional[str]:
    return _request_id.get()


def defaults() -> Dict[str, Any]:
    """Fields klog.emit merges in when the caller did not supply them."""
    return {
        "requestId": _request_id.get(),
        "sessionId": _session_id.get(),
        "userIdHash": _user_hash.get(),
        "route": _route.get(),
    }
