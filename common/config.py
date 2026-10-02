"""Per-service runtime configuration for CogniKart.

One container image serves all four CogniKart services; SERVICE_NAME selects
which router is mounted at startup (see cognikart/main.py). This keeps the
shared logging contract in one place and means a single Cloud Build produces
the image for all four Cloud Run services.
"""
import os
from typing import Dict, Optional

VALID_SERVICES = ("gateway", "catalog", "orders", "payments")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    """Resolved configuration for the service this process is running as."""

    def __init__(self) -> None:
        raw = _env("SERVICE_NAME", "gateway").lower()
        if raw not in VALID_SERVICES:
            raise ValueError(
                "SERVICE_NAME must be one of %s, got %r" % (", ".join(VALID_SERVICES), raw)
            )
        self.service_role: str = raw
        # Full name as it appears in logs and as the Cloud Run service name.
        self.service_name: str = "cognikart-%s" % raw
        self.service_version: str = _env("SERVICE_VERSION", "v1.0.0")
        self.environment: str = _env("ENVIRONMENT", "demo")
        self.port: int = int(_env("PORT", "8080"))

        # Downstream service URLs. Empty in single-service local runs; set by
        # scripts/run_local.sh and by the Cloud Run deploy step.
        self.catalog_url: str = _env("CATALOG_URL", "http://127.0.0.1:8081")
        self.orders_url: str = _env("ORDERS_URL", "http://127.0.0.1:8082")
        self.payments_url: str = _env("PAYMENTS_URL", "http://127.0.0.1:8083")

        # Local-mode log sink. When set, every log entry is also POSTed to the
        # platform so the dashboard works with no GCP project at all. On Cloud
        # Run this is left unset: stdout -> Cloud Logging is the real path.
        self.local_sink_url: str = _env("LOCAL_SINK_URL", "")

        # Dependency call timeout in seconds. Deliberately short so injected
        # latency produces real timeouts rather than just slow successes.
        self.dependency_timeout_s: float = float(_env("DEPENDENCY_TIMEOUT_S", "6.0"))
        # orders retries payments this many extra times. The retry storm this
        # creates under failure is the amplification the platform prices.
        self.payment_max_retries: int = int(_env("PAYMENT_MAX_RETRIES", "2"))

    def downstream(self, role: str) -> Optional[str]:
        return {
            "catalog": self.catalog_url,
            "orders": self.orders_url,
            "payments": self.payments_url,
        }.get(role)

    def as_dict(self) -> Dict[str, str]:
        return {
            "serviceName": self.service_name,
            "serviceRole": self.service_role,
            "serviceVersion": self.service_version,
            "environment": self.environment,
        }


settings = Settings()
