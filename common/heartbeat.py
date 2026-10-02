"""Per-service self-telemetry, emitted as a structured log every 10 seconds.

Why this exists: Cloud Monitoring samples Cloud Run metrics about once a
minute and then takes a few more minutes to make them readable through the
API. That is far too slow for a live demo -- a judge would watch a flat CPU
chart for five minutes after we inject a failure.

Logs, by contrast, are queryable within seconds. So each service measures its
own CPU, resident memory and in-flight request count with psutil and emits
them as a log line. The platform derives its LIVE resource tier from these
heartbeats (seconds) and uses Cloud Monitoring as the slower, independent
platform-truth tier (minutes). Both are real measurements; they just have
different latency and different provenance, and the dashboard labels which
is which.
"""
import threading
import time
from typing import Optional

import psutil

from . import events, logging as klog

_proc = psutil.Process()
_inflight = 0
_inflight_lock = threading.Lock()
_peak_rss_mb = 0.0
_started_at = time.time()


def inc_inflight() -> None:
    global _inflight
    with _inflight_lock:
        _inflight += 1


def dec_inflight() -> None:
    global _inflight
    with _inflight_lock:
        _inflight = max(0, _inflight - 1)


def current_inflight() -> int:
    return _inflight


def sample() -> dict:
    """Take one resource sample. cpu_percent(None) is non-blocking and
    reports usage since the previous call, which suits a fixed-interval loop."""
    global _peak_rss_mb
    rss_mb = round(_proc.memory_info().rss / (1024 * 1024), 2)
    _peak_rss_mb = max(_peak_rss_mb, rss_mb)
    return {
        "cpuPct": round(_proc.cpu_percent(None), 2),
        "rssMb": rss_mb,
        "peakRssMb": round(_peak_rss_mb, 2),
        "inflight": current_inflight(),
        "uptimeS": round(time.time() - _started_at, 1),
        "threads": _proc.num_threads(),
    }


def _loop(interval_s: float, chaos_name_fn) -> None:
    _proc.cpu_percent(None)  # prime the counter; first call always returns 0.0
    while True:
        time.sleep(interval_s)
        try:
            payload = sample()
            payload.update(klog.counters.snapshot())
            klog.emit(
                "INFO",
                "heartbeat cpu=%.1f%% rss=%.0fMB inflight=%d"
                % (payload["cpuPct"], payload["rssMb"], payload["inflight"]),
                events.SERVICE_HEARTBEAT,
                chaos_scenario=chaos_name_fn(),
                **payload
            )
        except Exception:
            pass  # a heartbeat must never take the service down


def start(interval_s: float = 10.0, chaos_name_fn=None) -> None:
    fn = chaos_name_fn or (lambda: None)
    threading.Thread(
        target=_loop, args=(interval_s, fn), daemon=True, name="heartbeat"
    ).start()
