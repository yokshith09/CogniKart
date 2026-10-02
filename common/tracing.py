"""Trace propagation across the four CogniKart services.

This is the highest-value 60 lines in CogniKart. Without a shared trace id,
"which service caused the problem?" is guesswork: the platform sees a 502 at
the gateway and an error at payments and cannot prove they are the same
request. With it, one checkout produces ~8 log entries across 4 services that
share one trace id, and the platform can render the whole causal chain.

We accept either W3C `traceparent` or Google's `X-Cloud-Trace-Context`
(which Cloud Run's load balancer injects), and always propagate downstream as
`traceparent`. The log field uses Cloud Logging's required resource-name
format: projects/PROJECT_ID/traces/TRACE_ID
"""
import os
import random
import re
from typing import Dict, Optional, Tuple

_TRACEPARENT_RE = re.compile(
    r"^00-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
# e.g. "105445aa7843bc8bf206b12000100000/1;o=1"
_XCTC_RE = re.compile(r"^(?P<trace>[0-9a-fA-F]{32})(?:/(?P<span>\d+))?")

PROJECT_ID = (
    os.environ.get("GOOGLE_CLOUD_PROJECT")
    or os.environ.get("GCP_PROJECT")
    or "local"
)


def _rand_hex(n: int) -> str:
    return "%0*x" % (n, random.getrandbits(n * 4))


def new_trace_id() -> str:
    return _rand_hex(32)


def new_span_id() -> str:
    return _rand_hex(16)


def parse_incoming(headers: Dict[str, str]) -> Tuple[str, Optional[str]]:
    """Return (trace_id, parent_span_id) from request headers, creating a new
    trace when the request carries none."""
    lower = {k.lower(): v for k, v in headers.items()}

    tp = lower.get("traceparent", "")
    m = _TRACEPARENT_RE.match(tp.strip())
    if m:
        return m.group("trace"), m.group("span")

    xctc = lower.get("x-cloud-trace-context", "")
    m = _XCTC_RE.match(xctc.strip())
    if m:
        return m.group("trace").lower(), None

    return new_trace_id(), None


def traceparent(trace_id: str, span_id: str) -> str:
    return "00-%s-%s-01" % (trace_id, span_id)


def trace_field(trace_id: str) -> str:
    """Cloud Logging's `logging.googleapis.com/trace` value."""
    return "projects/%s/traces/%s" % (PROJECT_ID, trace_id)


def outbound_headers(trace_id: str, span_id: str, request_id: str) -> Dict[str, str]:
    """Headers to attach when calling a downstream CogniKart service."""
    return {
        "traceparent": traceparent(trace_id, span_id),
        "X-Request-Id": request_id,
        "X-CogniKart-Internal": "1",
    }
