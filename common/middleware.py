"""Request middleware shared by all four CogniKart services.

Every request produces exactly one `http.request.completed` or
`http.request.failed` log line carrying the Cloud Logging `httpRequest` block,
the trace id, measured latency and (on failure) a groupable error code. This
uniformity is what makes centralised logging actually usable: the platform can
compute request rate, latency percentiles and error rate for any service
without knowing anything service-specific.

Health checks are deliberately excluded. Cloud Run probes them constantly and
including them would inflate the request-rate baseline and dilute the error
ratio, making every threshold meaningless.
"""
import time
import uuid
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import chaos, context, events, heartbeat, logging as klog, tracing

EXCLUDED_PATHS = frozenset({"/healthz", "/readyz", "/favicon.ico"})

# Serving the storefront's own HTML/CSS/JS is not application work. Counting
# those requests would inflate the request-rate baseline and dilute the error
# ratio, making every alert threshold less meaningful. The API calls the page
# makes are the signal; the asset fetches are not.
EXCLUDED_PREFIXES = ("/static/",)


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


def _http_request_block(request: Request, status: int, latency_s: float, size: int) -> Dict[str, Any]:
    return {
        "requestMethod": request.method,
        "requestUrl": str(request.url.path),
        "status": status,
        "latency": "%.3fs" % latency_s,
        "responseSize": size,
        "userAgent": request.headers.get("user-agent", ""),
        "remoteIp": request.client.host if request.client else "",
    }


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe(request: Request, call_next: Callable) -> Any:
        path = request.url.path
        if path in EXCLUDED_PATHS or path.startswith(EXCLUDED_PREFIXES):
            return await call_next(request)

        trace_id, _parent = tracing.parse_incoming(dict(request.headers))
        span_id = tracing.new_span_id()
        request_id = request.headers.get("x-request-id") or new_request_id()

        # Make these available to handlers without threading them through
        # every function signature.
        request.state.trace_id = trace_id
        request.state.span_id = span_id
        request.state.request_id = request_id

        # Every log line emitted while handling this request now inherits the
        # trace, span and request id automatically.
        context.set_request(
            trace_id=tracing.trace_field(trace_id),
            span_id=span_id,
            request_id=request_id,
            session_id=request.headers.get("x-session-id"),
            user_hash=request.headers.get("x-user-hash"),
            route=path,
        )

        heartbeat.inc_inflight()
        started = time.time()
        status = 500
        response = None
        failure: Optional[Dict[str, Any]] = None
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:  # noqa: BLE001 - we log then re-raise as 500
            failure = {
                "errorCode": events.ErrorCode.INTERNAL_ERROR,
                "errorClass": type(exc).__name__,
                "detail": str(exc),
            }
            response = JSONResponse(
                status_code=500,
                content={"error": "INTERNAL_ERROR", "requestId": request_id},
            )
            status = 500
        finally:
            heartbeat.dec_inflight()

        latency_s = time.time() - started
        latency_ms = int(latency_s * 1000)
        size = int(response.headers.get("content-length") or 0)

        # Error code set by the handler (via request.state) takes precedence:
        # the handler knows why it failed, the middleware only knows that it did.
        handler_code = getattr(request.state, "error_code", None)
        handler_class = getattr(request.state, "error_class", None)
        if failure:
            code, klass = failure["errorCode"], failure["errorClass"]
        else:
            code, klass = handler_code, handler_class

        severity = "INFO"
        if status >= 500:
            severity = "ERROR"
        elif status >= 400 or latency_ms > 1000:
            severity = "WARNING"

        event = events.HTTP_REQUEST_COMPLETED if status < 400 else events.HTTP_REQUEST_FAILED
        msg = "%s %s -> %d in %dms" % (request.method, path, status, latency_ms)

        klog.emit(
            severity,
            msg,
            event,
            trace=tracing.trace_field(trace_id),
            span_id=span_id,
            http_request=_http_request_block(request, status, latency_s, size),
            chaos_scenario=chaos.current_scenario(),
            route=path,
            httpStatus=status,
            latencyMs=latency_ms,
            requestId=request_id,
            sessionId=request.headers.get("x-session-id"),
            userIdHash=request.headers.get("x-user-hash"),
            errorCode=code,
            errorClass=klass,
            dependency=getattr(request.state, "dependency", None),
            dependencyLatencyMs=getattr(request.state, "dependency_latency_ms", None),
            retryCount=getattr(request.state, "retry_count", None),
            orderId=getattr(request.state, "order_id", None),
            cartValueInr=getattr(request.state, "cart_value_inr", None),
            skuCount=getattr(request.state, "sku_count", None),
            stockShortfall=getattr(request.state, "stock_shortfall", None),
        )

        if response is not None:
            response.headers["X-Request-Id"] = request_id
            response.headers["X-Trace-Id"] = trace_id
        return response
