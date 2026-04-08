"""
Analysis routes — batch pipeline execution and report retrieval.

  POST /api/analyze              run the full pipeline on a metric batch
  GET  /api/report/latest        fetch the most recent report
  GET  /api/reports              report history (paginated)
  GET  /api/metrics/history      raw timeseries query (DuckDB only)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Query, Request, Security
from slowapi import Limiter

from app import deps
from app.models import AnalyzeRequest, Report
from app.security import is_private, limiter, require_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analysis"])

_ANALYZE_EXAMPLE = {
    "minimal": {
        "summary": "Two data points",
        "value": {
            "metrics": [
                {
                    "timestamp": "2023-10-01T12:00:00Z",
                    "cpu_usage": 93, "memory_usage": 86,
                    "latency_ms": 334, "disk_usage": 89,
                    "network_in_kbps": 2541, "network_out_kbps": 2137,
                    "io_wait": 12, "thread_count": 143,
                    "active_connections": 126, "error_rate": 0.12,
                    "uptime_seconds": 360000, "temperature_celsius": 84,
                    "power_consumption_watts": 356,
                    "service_status": {
                        "database": "online",
                        "api_gateway": "degraded",
                        "cache": "online",
                    },
                }
            ],
            "webhooks": [],
        },
    }
}


@router.post(
    "/api/analyze",
    response_model=Report,
    summary="Run full analysis pipeline",
    description="""
Submit a batch of metric points and run the complete pipeline:
**ingest → enrich → analyze → recommend → report**.

The pipeline runs in a background thread so the event loop stays free.
Set `PIPELINE_TIMEOUT` (default 60 s) to cap LLM wait time.

If `recommendation_error` is set in the response summary, the LLM call failed
(check your `ANTHROPIC_API_KEY`) — the rest of the report is still valid.
""",
    response_description="Full structured report with anomalies and recommendations.",
    responses={
        401: {"description": "Missing or invalid X-API-Key."},
        422: {"description": "Payload exceeds MAX_METRICS or fails field validation."},
        429: {"description": "Rate limit exceeded (RATE_LIMIT env var)."},
        504: {"description": "Pipeline timed out (PIPELINE_TIMEOUT env var)."},
    },
)
@limiter.limit(os.getenv("RATE_LIMIT", "10/minute"))
async def analyze(
    request: Request,
    body: AnalyzeRequest = Body(openapi_examples=_ANALYZE_EXAMPLE),
    _: None = Security(require_key),
) -> Report:
    max_metrics = int(os.getenv("MAX_METRICS", 5000))
    if len(body.metrics) > max_metrics:
        raise HTTPException(
            status_code=422,
            detail=f"Too many data points ({len(body.metrics)}). Max: {max_metrics}.",
        )

    report = await deps.run_pipeline(body)

    all_urls = list(body.webhooks) + [
        v.strip() for v in os.getenv("WEBHOOK_URLS", "").split(",") if v.strip()
    ]
    urls = [u for u in all_urls if u and not is_private(u)]
    if urls and deps.http_client:
        payload = report.model_dump()
        for url in urls:
            try:
                await deps.http_client.post(url, json=payload)
            except Exception as exc:
                logger.warning("WRNRAN000 webhook_failed", extra={"url": url, "error": str(exc)})

    return report


@router.get(
    "/api/report/latest",
    response_model=Report,
    summary="Get latest report",
    description="Returns the most recently generated report. Poll this endpoint to refresh the dashboard.",
    response_description="The latest report, or 404 if none has been generated yet.",
    responses={
        404: {"description": "No report generated yet."},
        401: {"description": "Missing or invalid X-API-Key."},
    },
)
def latest_report(_: None = Security(require_key)) -> Report:
    report = deps.store.latest()
    if report is None:
        raise HTTPException(status_code=404, detail="No report yet — POST /api/analyze first.")
    return report


@router.get(
    "/api/reports",
    response_model=list[Report],
    summary="Report history",
    description="Returns the last `n` reports in descending order. Useful for trend analysis and audit trails.",
    response_description="List of reports, most recent first.",
    responses={401: {"description": "Missing or invalid X-API-Key."}},
)
def report_history(
    n: int = Query(default=20, ge=1, le=100, description="Number of reports to return (max 100)."),
    _: None = Security(require_key),
) -> list[Report]:
    return deps.store.history(n)


@router.get(
    "/api/metrics/history",
    response_model=list[dict],
    tags=["analysis"],
    summary="Raw metric timeseries query",
    description="""
Query raw metric data points stored across multiple analysis runs.
**Requires DuckDB store** (`STORE_DSN=./data/metrics.duckdb`).

With SQLite or MemoryStore this returns an empty list.

### Example queries
- All data for a specific host: `?host=web-01`
- Last 24 hours: `?start=2023-10-02T00:00:00Z`
- Specific window: `?start=2023-10-01T12:00:00Z&end=2023-10-01T18:00:00Z`

Use this endpoint to feed Grafana panels, build custom dashboards,
or re-run analysis on historical data with updated thresholds.
""",
    response_description="Raw metric rows ordered by timestamp ASC.",
    responses={401: {"description": "Missing or invalid X-API-Key."}},
)
def metrics_history(
    host:  str | None      = Query(default=None, description="Filter by host/server name."),
    start: datetime | None = Query(default=None, description="Start of time range (ISO 8601)."),
    end:   datetime | None = Query(default=None, description="End of time range (ISO 8601)."),
    limit: int             = Query(default=1000, ge=1, le=10000, description="Max rows to return."),
    _: None = Security(require_key),
) -> list[dict]:
    return deps.store.metric_history(host=host, start=start, end=end, limit=limit)
