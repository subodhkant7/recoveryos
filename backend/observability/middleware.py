"""
Correlation ID and Metrics HTTP Middleware.

Attaches request correlation identifiers, records latency histograms,
and exports request metrics with strict cardinality control.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.observability.logging import current_request_id
from backend.observability.metrics import metrics

logger = logging.getLogger("recoveryos.http.access")
SAFE_REQUEST_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _sanitize_path_for_metric(path: str) -> str:
    """Normalize dynamic path parameters to prevent label cardinality explosion."""
    # Replace UUIDs
    p = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ":id", path)
    # Replace approval IDs
    p = re.sub(r"appr-[0-9a-zA-Z]+", ":approval_id", p)
    # Replace scenario names
    p = re.sub(r"/api/scenarios/[^/]+", "/api/scenarios/:scenario", p)
    return p


class CorrelationAndMetricsMiddleware(BaseHTTPMiddleware):
    """
    HTTP middleware managing request correlation IDs, metrics counters, and access logging.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Extract or generate request ID
        header_req_id = request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID")
        if header_req_id and SAFE_REQUEST_ID_REGEX.match(header_req_id):
            req_id = header_req_id
        else:
            req_id = f"req-{uuid.uuid4().hex[:12]}"

        token = current_request_id.set(req_id)
        start_time = time.monotonic()

        norm_path = _sanitize_path_for_metric(request.url.path)
        method = request.method

        try:
            response = await call_next(request)
            status_code = str(response.status_code)
        except Exception as exc:
            status_code = "500"
            duration = time.monotonic() - start_time
            metrics.inc_counter(
                "recoveryos_http_requests_total",
                labels={"method": method, "endpoint": norm_path, "status": status_code},
            )
            metrics.observe_histogram(
                "recoveryos_http_request_duration_seconds",
                duration,
                labels={"method": method, "endpoint": norm_path},
            )
            logger.error(f"HTTP {method} {request.url.path} -> 500 error ({duration:.4f}s): {exc}")
            current_request_id.reset(token)
            raise

        duration = time.monotonic() - start_time
        metrics.inc_counter(
            "recoveryos_http_requests_total",
            labels={"method": method, "endpoint": norm_path, "status": status_code},
        )
        metrics.observe_histogram(
            "recoveryos_http_request_duration_seconds",
            duration,
            labels={"method": method, "endpoint": norm_path},
        )

        response.headers["X-Request-ID"] = req_id
        current_request_id.reset(token)
        return response
