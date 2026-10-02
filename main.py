"""CogniKart entrypoint. One image, four services.

SERVICE_NAME selects which router is mounted. This means a single Cloud Build
produces the image for all four Cloud Run services, the shared log schema
needs no packaging tricks, and `gcloud run deploy` runs four times against one
already-built image instead of rebuilding four times.
"""
import os
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .common import admin, chaos, heartbeat, logging as klog, middleware
from .common.config import settings
from .services import catalog, gateway, orders, payments

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_ROUTERS = {
    "gateway": (gateway.router, gateway.startup),
    "catalog": (catalog.router, catalog.startup),
    "orders": (orders.router, orders.startup),
    "payments": (payments.router, payments.startup),
}


def _service_info(role: str) -> Dict[str, Any]:
    payload = {"service": settings.service_name, "role": role}
    payload.update(settings.as_dict())
    payload["chaos"] = chaos.state().as_dict()
    return payload


def create_app() -> FastAPI:
    role = settings.service_role
    router, on_start = _ROUTERS[role]

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        description="CogniKart demo e-commerce service (%s role). Generates "
                    "realistic telemetry for the OpsMind observability "
                    "platform." % role,
    )
    middleware.install(app)
    app.include_router(router)
    app.include_router(admin.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Dict[str, Any]:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Dict[str, Any]:
        return {"status": "ready", "service": settings.service_name}

    # Only the gateway serves the storefront. The internal services stay
    # API-only, which keeps their telemetry clean and their images identical.
    if role == "gateway" and os.path.isdir(_STATIC):
        app.mount("/static", StaticFiles(directory=_STATIC), name="static")

        @app.get("/", include_in_schema=False)
        def storefront() -> Any:
            index = os.path.join(_STATIC, "index.html")
            if os.path.exists(index):
                return FileResponse(index)
            return JSONResponse(_service_info(role))
    else:
        @app.get("/", include_in_schema=False)
        def root() -> Dict[str, Any]:
            return _service_info(role)

    @app.on_event("startup")
    def _on_startup() -> None:
        on_start()
        # Heartbeat gives the platform a LIVE (seconds) resource signal, since
        # Cloud Monitoring metrics lag by minutes. See common/heartbeat.py.
        heartbeat.start(interval_s=10.0, chaos_name_fn=chaos.current_scenario)
        klog.info(
            "%s listening on :%d (log level %s, sink %s)"
            % (settings.service_name, settings.port, klog.get_min_severity(),
               settings.local_sink_url or "stdout-only"),
            "service.started",
        )

    return app


app = create_app()
