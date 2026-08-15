from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .observability import MetricsRegistry, metrics


class LegacyCloudAPICompatibilityMiddleware(BaseHTTPMiddleware):
    """Count requests accepted by the temporary unversioned edge route."""

    def __init__(
        self,
        app,
        *,
        registry: MetricsRegistry = metrics,
    ) -> None:
        super().__init__(app)
        self.registry = registry

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.headers.get("x-lumenx-legacy-api") == "1":
            self.registry.increment(
                "cloud_legacy_api_requests_total",
                labels={"operation": "edge_compatibility"},
            )
        return await call_next(request)
