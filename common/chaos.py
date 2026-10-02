"""Controlled failure injection.

Requirements this satisfies, in order of importance for a live demo:
  deterministic  -- a named scenario always produces the same behaviour
  parameterised  -- fixed intensities, written down, not random
  reversible     -- `recover-all` returns every service to baseline
  self-expiring  -- every scenario has a hard expiry, so a forgotten DEBUG
                    flag cannot quietly burn through the Cloud Logging free
                    allotment overnight

INTEGRITY RULE: while chaos is active, log lines carry a `chaosScenario`
field. The monitoring platform must never read that field to detect anything.
It exists so we can measure our own detector after the fact ("alert fired 47s
after injection") and so the demo is auditable. If the platform detected
incidents by reading this field the whole demo would be circular.
"""
import random
import threading
import time
from typing import Any, Dict, List, Optional

from . import events, logging as klog

# Hard ceilings. A scenario may ask for less, never more.
_MAX_DURATION_S = 1800          # 30 min: nothing stays broken past this
_MAX_DEBUG_DURATION_S = 600     # 10 min: DEBUG is the log-volume risk
_MAX_LEAK_MB = 400              # don't OOM a dev laptop

_lock = threading.Lock()
_leaked: List[bytearray] = []


class ChaosState:
    """Mutable injection knobs for this service instance."""

    def __init__(self) -> None:
        self.scenario: Optional[str] = None
        self.error_rate: float = 0.0
        self.extra_latency_ms: int = 0
        self.latency_ramp_to_ms: int = 0
        self.ramp_seconds: int = 0
        self.stock_conflict_rate: float = 0.0
        self.memory_leak_kb: int = 0
        self.log_level: Optional[str] = None
        self.applied_at: float = 0.0
        self.expires_at: float = 0.0

    def is_active(self) -> bool:
        return self.scenario is not None

    def as_dict(self) -> Dict[str, Any]:
        remaining = max(0.0, self.expires_at - time.time()) if self.is_active() else 0.0
        return {
            "scenario": self.scenario,
            "active": self.is_active(),
            "errorRate": self.error_rate,
            "extraLatencyMs": self.extra_latency_ms,
            "latencyRampToMs": self.latency_ramp_to_ms,
            "rampSeconds": self.ramp_seconds,
            "stockConflictRate": self.stock_conflict_rate,
            "memoryLeakKb": self.memory_leak_kb,
            "logLevel": self.log_level or klog.get_min_severity(),
            "appliedAt": self.applied_at or None,
            "expiresAt": self.expires_at or None,
            "secondsRemaining": round(remaining, 1),
            "leakedMb": round(sum(len(b) for b in _leaked) / (1024 * 1024), 1),
        }


_state = ChaosState()


def _expire_if_due() -> None:
    """Lazily enforce expiry on every read. No background timer needed, and
    it cannot be defeated by a missed tick."""
    if _state.is_active() and time.time() >= _state.expires_at:
        name = _state.scenario
        _clear_locked()
        klog.info("chaos scenario %s expired automatically" % name, events.CHAOS_EXPIRED,
                  chaosScenario=name)


def state() -> ChaosState:
    with _lock:
        _expire_if_due()
        return _state


def current_scenario() -> Optional[str]:
    with _lock:
        _expire_if_due()
        return _state.scenario


def _clear_locked() -> None:
    global _leaked
    _state.__init__()  # reset all knobs to baseline
    _leaked = []
    klog.set_min_severity("INFO")


def clear() -> Dict[str, Any]:
    with _lock:
        was = _state.scenario
        _clear_locked()
    if was:
        klog.info("chaos cleared (was %s)" % was, events.CHAOS_CLEARED)
    return state().as_dict()


def apply(knobs: Dict[str, Any]) -> Dict[str, Any]:
    """Apply injection knobs to this service. Unknown keys are ignored."""
    duration = int(knobs.get("durationS") or 600)
    requested_level = (knobs.get("logLevel") or "").upper() or None
    cap = _MAX_DEBUG_DURATION_S if requested_level == "DEBUG" else _MAX_DURATION_S
    duration = max(10, min(duration, cap))

    with _lock:
        _state.scenario = knobs.get("scenario") or "custom"
        _state.error_rate = max(0.0, min(1.0, float(knobs.get("errorRate") or 0.0)))
        _state.extra_latency_ms = max(0, int(knobs.get("extraLatencyMs") or 0))
        _state.latency_ramp_to_ms = max(0, int(knobs.get("latencyRampToMs") or 0))
        _state.ramp_seconds = max(0, int(knobs.get("rampSeconds") or 0))
        _state.stock_conflict_rate = max(0.0, min(1.0, float(knobs.get("stockConflictRate") or 0.0)))
        _state.memory_leak_kb = max(0, int(knobs.get("memoryLeakKb") or 0))
        _state.log_level = requested_level
        _state.applied_at = time.time()
        _state.expires_at = _state.applied_at + duration
        if requested_level:
            klog.set_min_severity(requested_level)
        snapshot = _state.as_dict()

    klog.warning(
        "chaos scenario applied: %s (expires in %ds)" % (snapshot["scenario"], duration),
        events.CHAOS_APPLIED,
        chaosScenario=snapshot["scenario"],
        chaosKnobs=snapshot,
    )
    return snapshot


# --- Effects, called from request handlers ---------------------------------

def effective_latency_ms() -> int:
    """Flat injected latency, or the current point on a ramp."""
    st = state()
    if not st.is_active():
        return 0
    if st.latency_ramp_to_ms and st.ramp_seconds:
        elapsed = time.time() - st.applied_at
        frac = max(0.0, min(1.0, elapsed / float(st.ramp_seconds)))
        start = st.extra_latency_ms
        return int(start + (st.latency_ramp_to_ms - start) * frac)
    return st.extra_latency_ms


# Jitter band applied to injected latency. Real dependency latency is never a
# constant, and a constant here would make the cascade degenerate: either every
# call times out or none does. With a 4s base and a 6s caller timeout this band
# produces a realistic mix of genuine timeouts, 503s and slow successes -- which
# is a far richer signal for the platform to correlate.
_JITTER_LO, _JITTER_HI = 0.5, 1.8


def inject_latency() -> int:
    ms = effective_latency_ms()
    if ms <= 0:
        return 0
    ms = int(ms * random.uniform(_JITTER_LO, _JITTER_HI))
    time.sleep(ms / 1000.0)
    return ms


def should_fail() -> bool:
    st = state()
    return st.is_active() and st.error_rate > 0 and random.random() < st.error_rate


def should_stock_conflict() -> bool:
    st = state()
    return (
        st.is_active()
        and st.stock_conflict_rate > 0
        and random.random() < st.stock_conflict_rate
    )


def leak_memory() -> int:
    """Retain memory per request, to drive a slow-burn memory alert."""
    st = state()
    if not st.is_active() or st.memory_leak_kb <= 0:
        return 0
    with _lock:
        held_mb = sum(len(b) for b in _leaked) / (1024 * 1024)
        if held_mb >= _MAX_LEAK_MB:
            return int(held_mb)
        _leaked.append(bytearray(st.memory_leak_kb * 1024))
        return int(held_mb)


# --- Named scenarios -------------------------------------------------------
# Each entry maps a scenario name to the knobs each service should receive.
# The gateway orchestrates: one API call fans these out to the right services.
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "payment-timeout-cascade": {
        "description": (
            "payments injects 4s latency and fails 60% of authorizations; orders "
            "retries twice with no backoff, tripling request and log volume; the "
            "gateway surfaces 502s. Demonstrates cascade, amplification and the "
            "cost of an incident."
        ),
        "priority": "P0",
        "defaultDurationS": 600,
        "targets": {
            "payments": {"errorRate": 0.6, "extraLatencyMs": 4000},
        },
    },
    "debug-logging-left-on": {
        "description": (
            "catalog log level drops to DEBUG, multiplying log volume roughly 6x "
            "with zero errors. Demonstrates pure cost waste with a flat error "
            "graph -- the scenario that proves logs and cost are one product."
        ),
        "priority": "P0",
        "defaultDurationS": 420,
        "targets": {
            "catalog": {"logLevel": "DEBUG"},
        },
    },
    "traffic-spike-3x": {
        "description": (
            "No fault injected. Run loadgen with --profile spike. Instance count "
            "and cost rise with no errors, proving the platform distinguishes "
            "'expensive' from 'broken'."
        ),
        "priority": "P0",
        "defaultDurationS": 300,
        "targets": {},
        "loadgenProfile": "spike",
    },
    "catalog-memory-leak": {
        "description": (
            "catalog retains ~2MB per request. Memory climbs toward the container "
            "limit, driving a slow-burn memory alert and eventual instance churn."
        ),
        "priority": "P1",
        "defaultDurationS": 900,
        "targets": {
            "catalog": {"memoryLeakKb": 2048},
        },
    },
    "stock-race-errors": {
        "description": (
            "catalog returns STOCK_CONFLICT on 35% of reservations. A WARNING "
            "flood with few 5xx: log-volume cost rises while the error-rate alert "
            "stays quiet. Demonstrates alert-design nuance."
        ),
        "priority": "P1",
        "defaultDurationS": 600,
        "targets": {
            "catalog": {"stockConflictRate": 0.35},
        },
    },
    "slow-dependency-degradation": {
        "description": (
            "payments latency ramps from 200ms to 3s over 10 minutes. A static "
            "threshold misses this; a trend rule catches it."
        ),
        "priority": "P2",
        "defaultDurationS": 900,
        "targets": {
            "payments": {"extraLatencyMs": 200, "latencyRampToMs": 3000, "rampSeconds": 600},
        },
    },
    "recover-all": {
        "description": "Clear all injected state everywhere and return to baseline.",
        "priority": "P0",
        "defaultDurationS": 0,
        "targets": {},
    },
}


def scenario_catalog() -> List[Dict[str, Any]]:
    out = []
    for name, spec in SCENARIOS.items():
        out.append({
            "name": name,
            "description": spec["description"],
            "priority": spec["priority"],
            "defaultDurationS": spec["defaultDurationS"],
            "affects": sorted(spec["targets"].keys()) or ["(loadgen only)"],
            "loadgenProfile": spec.get("loadgenProfile"),
        })
    return out
