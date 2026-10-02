"""Chaos-control endpoints, mounted on every CogniKart service.

Each service exposes its own knobs; the gateway additionally exposes a
scenario endpoint that fans out to the right services so one call produces one
coherent, named failure (see cognikart/services/gateway.py).
"""
from typing import Any, Dict

from fastapi import APIRouter, Body

from . import chaos, logging as klog
from .config import settings

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/chaos")
def get_chaos() -> Dict[str, Any]:
    return {
        "service": settings.service_name,
        "state": chaos.state().as_dict(),
        "logLevel": klog.get_min_severity(),
    }


@router.post("/chaos")
def set_chaos(knobs: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    return {"service": settings.service_name, "state": chaos.apply(knobs)}


@router.post("/chaos/reset")
def reset_chaos() -> Dict[str, Any]:
    return {"service": settings.service_name, "state": chaos.clear()}


@router.get("/stats")
def stats() -> Dict[str, Any]:
    """Self-reported counters. The platform uses logBytesTotal to price Cloud
    Logging ingestion from measured volume rather than an estimate."""
    from . import heartbeat
    payload = {"service": settings.service_name}
    payload.update(settings.as_dict())
    payload.update(heartbeat.sample())
    payload.update(klog.counters.snapshot())
    payload["chaos"] = chaos.state().as_dict()
    return payload
