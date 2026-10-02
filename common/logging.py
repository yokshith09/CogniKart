"""Structured JSON logging for CogniKart. This module IS the log schema.

Design notes (see docs/LOG_SCHEMA.md for the full contract):

* We write one JSON object per line to stdout. On Cloud Run the logging agent
  picks this up with no agent install and no IAM — that is why "capture logs"
  and "send logs to a cloud service" are configuration, not code.
* Four field names are special to Cloud Logging and are promoted out of the
  JSON payload into real log-entry structure. We use their exact spellings:
      severity                            -> entry severity (enables severity>= filters)
      message                             -> entry summary line
      time                                -> entry timestamp (RFC3339)
      httpRequest                         -> structured HTTP block
      logging.googleapis.com/trace        -> trace correlation across services
      logging.googleapis.com/spanId       -> span within a trace
  Getting `severity` wrong silently breaks every severity filter downstream,
  so it is validated here rather than trusted to callers.
* Application fields use camelCase to match docs/architecture.md section 5.
* Redaction runs before emit, in-process, so PII never leaves the container.
* We account for our own emitted bytes. The platform's cost engine needs log
  volume to price Cloud Logging ingestion, and measuring it at the source is
  more accurate than estimating it from sampled reads.
"""
import json
import os
import queue
import re
import sys
import threading
import time as _time
from typing import Any, Dict, Optional

from . import context
from .config import settings

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# --- Redaction -------------------------------------------------------------
# Keys whose values must never be logged, at any severity, ever.
_DENY_KEYS = frozenset({
    "password", "passwd", "secret", "token", "authorization", "auth",
    "apikey", "api_key", "cookie", "sessionsecret", "cardnumber",
    "card_number", "pan", "cvv", "cvc", "email", "phone", "address",
    "fullname", "full_name", "firstname", "lastname", "ssn", "aadhaar",
})
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 12-19 consecutive digits, optionally space/hyphen separated: card-like.
_LONGNUM_RE = re.compile(r"\b(?:\d[ \-]?){12,19}\b")
_REDACTED = "[REDACTED]"

_MAX_STRING_LEN = 1024  # truncate long values; stack traces are the usual culprit


def _scrub_text(value: str) -> str:
    value = _EMAIL_RE.sub(_REDACTED, value)
    value = _LONGNUM_RE.sub(_REDACTED, value)
    if len(value) > _MAX_STRING_LEN:
        value = value[:_MAX_STRING_LEN] + "...[truncated]"
    return value


def redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively strip denylisted keys and scrub PII-shaped strings."""
    if _depth > 6:
        return "[max-depth]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower().replace("-", "").replace("_", "") in _DENY_KEYS:
                out[k] = _REDACTED
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj][:50]
    if isinstance(obj, str):
        return _scrub_text(obj)
    return obj


# --- Local sink ------------------------------------------------------------
# In local mode we also POST each entry to the platform so the dashboard works
# with no GCP project. A daemon thread drains a bounded queue so a slow or
# absent platform can never block request handling or grow memory without
# limit. On Cloud Run LOCAL_SINK_URL is unset and this is entirely inert.
_sink_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=4000)
_sink_thread_started = False
_sink_dropped = 0


def _sink_worker() -> None:
    import httpx  # imported lazily: not needed when no sink is configured

    url = settings.local_sink_url.rstrip("/") + "/internal/ingest"
    client = httpx.Client(timeout=3.0)
    batch = []
    while True:
        try:
            batch.append(_sink_q.get(timeout=0.5))
            while len(batch) < 100:
                try:
                    batch.append(_sink_q.get_nowait())
                except queue.Empty:
                    break
        except queue.Empty:
            pass
        if not batch:
            continue
        try:
            client.post(url, json={"entries": batch})
        except Exception:
            pass  # the sink is best-effort; stdout remains the source of truth
        batch = []


def _ensure_sink() -> None:
    global _sink_thread_started
    if _sink_thread_started or not settings.local_sink_url:
        return
    _sink_thread_started = True
    threading.Thread(target=_sink_worker, daemon=True, name="local-log-sink").start()


# --- Volume accounting -----------------------------------------------------
class _Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bytes_total = 0
        self.lines_total = 0
        self.by_severity = {s: 0 for s in SEVERITIES}

    def add(self, severity: str, nbytes: int) -> None:
        with self.lock:
            self.bytes_total += nbytes
            self.lines_total += 1
            self.by_severity[severity] = self.by_severity.get(severity, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "logBytesTotal": self.bytes_total,
                "logLinesTotal": self.lines_total,
                "logLinesBySeverity": dict(self.by_severity),
                "logSinkDropped": _sink_dropped,
            }


counters = _Counters()

_INSTANCE_ID = os.environ.get("CLOUD_RUN_EXECUTION") or ("local-%d" % os.getpid())

# DEBUG is suppressed by default. The `debug-logging-left-on` chaos scenario
# flips this at runtime; it is the single biggest log-volume (and therefore
# cost) lever in the system, which is exactly why it makes a good demo.
_min_severity = os.environ.get("LOG_LEVEL", "INFO").upper()
if _min_severity not in _SEVERITY_RANK:
    _min_severity = "INFO"


def set_min_severity(level: str) -> str:
    """Change the emit threshold at runtime. Returns the level actually set."""
    global _min_severity
    level = (level or "").upper()
    if level in _SEVERITY_RANK:
        _min_severity = level
    return _min_severity


def get_min_severity() -> str:
    return _min_severity


def _iso_now() -> str:
    # RFC3339 with milliseconds and explicit Z, which is what Cloud Logging wants.
    t = _time.time()
    ms = int((t % 1) * 1000)
    return _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(t)) + ".%03dZ" % ms


def emit(
    severity: str,
    message: str,
    event: str,
    *,
    trace: Optional[str] = None,
    span_id: Optional[str] = None,
    http_request: Optional[Dict[str, Any]] = None,
    chaos_scenario: Optional[str] = None,
    **fields: Any
) -> Optional[Dict[str, Any]]:
    """Emit one structured log line. Returns the entry, or None if suppressed."""
    severity = (severity or "INFO").upper()
    if severity not in _SEVERITY_RANK:
        severity = "INFO"
    if _SEVERITY_RANK[severity] < _SEVERITY_RANK[_min_severity]:
        return None

    # Trace/span/request default to the current request context, so business
    # events are trace-correlated without every call site passing them.
    if trace is None:
        ctx_trace = context.trace_id()
        if ctx_trace:
            trace = ctx_trace
    if span_id is None:
        span_id = context.span_id()

    entry: Dict[str, Any] = {
        "severity": severity,
        "message": _scrub_text(str(message)),
        "time": _iso_now(),
        "service": settings.service_name,
        "serviceRole": settings.service_role,
        "serviceVersion": settings.service_version,
        "environment": settings.environment,
        "event": event,
        "instanceId": _INSTANCE_ID,
    }
    if trace:
        entry["logging.googleapis.com/trace"] = trace
    if span_id:
        entry["logging.googleapis.com/spanId"] = span_id
    if http_request:
        entry["httpRequest"] = redact(http_request)
    if chaos_scenario:
        # Audit trail only. The platform MUST NOT read this field for
        # detection -- see docs/LOG_SCHEMA.md "Integrity rule".
        entry["chaosScenario"] = chaos_scenario

    for k, v in context.defaults().items():
        if v is not None and k not in fields:
            entry[k] = v
    for k, v in fields.items():
        if v is not None:
            entry[k] = v
    entry = redact(entry)

    line = json.dumps(entry, separators=(",", ":"), default=str)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    counters.add(severity, len(line) + 1)

    if settings.local_sink_url:
        _ensure_sink()
        global _sink_dropped
        try:
            _sink_q.put_nowait(entry)
        except queue.Full:
            _sink_dropped += 1
    return entry


def debug(message: str, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
    return emit("DEBUG", message, event, **kw)


def info(message: str, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
    return emit("INFO", message, event, **kw)


def warning(message: str, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
    return emit("WARNING", message, event, **kw)


def error(message: str, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
    return emit("ERROR", message, event, **kw)


def critical(message: str, event: str, **kw: Any) -> Optional[Dict[str, Any]]:
    return emit("CRITICAL", message, event, **kw)
