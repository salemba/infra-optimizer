"""
Streaming routes — real-time metric ingestion and Server-Sent Events.

  POST   /api/metrics           push points to the in-memory buffer
  GET    /api/metrics/buffer    buffer status
  DELETE /api/metrics/buffer    clear the buffer
  POST   /api/metrics/analyze   drain buffer and run the pipeline
  GET    /api/events            SSE stream of reports
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Body, HTTPException, Request, Security
from fastapi.responses import StreamingResponse

from app import buffer, deps
from app.models import AnalyzeRequest, BufferStatus, MetricPoint
from app.security import limiter, require_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])

_SINGLE_POINT_EXAMPLE = {
    "single": {
        "summary": "Single point",
        "value": [
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
    }
}


@router.post(
    "/api/metrics",
    status_code=202,
    summary="Push metric points to the buffer",
    description="""
Ingest one or more metric points into the in-memory streaming buffer.
Returns `202 Accepted` immediately — no analysis is triggered yet.

Once enough points have accumulated, call **POST /api/metrics/analyze**
to run the pipeline, or subscribe to **GET /api/events** to receive
reports automatically when analysis completes.

Buffer capacity is set by `BUFFER_SIZE` (default 1000). Returns 429 when full.
""",
    response_description="Number of points now in the buffer.",
    responses={
        401: {"description": "Missing or invalid X-API-Key."},
        429: {"description": "Buffer full or rate limit exceeded."},
    },
)
@limiter.limit(os.getenv("INGEST_RATE_LIMIT", "120/minute"))
async def ingest_metrics(
    request: Request,
    points: list[MetricPoint] = Body(
        description="One or more metric data points.",
        openapi_examples=_SINGLE_POINT_EXAMPLE,
    ),
    _: None = Security(require_key),
) -> dict:
    with buffer._buffer_lock:
        remaining = buffer.BUFFER_CAP - len(buffer._metric_buffer)
        if remaining <= 0:
            raise HTTPException(
                status_code=429,
                detail=f"Buffer full ({buffer.BUFFER_CAP} points). Call POST /api/metrics/analyze or DELETE /api/metrics/buffer.",
            )
        to_add = points[:remaining]
        buffer._metric_buffer.extend(to_add)
        size = len(buffer._metric_buffer)

    logger.info("INFRST000 buffer_ingest", extra={"added": len(to_add), "total": size})
    return {"buffered": size, "capacity": buffer.BUFFER_CAP}


@router.get(
    "/api/metrics/buffer",
    response_model=BufferStatus,
    summary="Buffer status",
    description="Returns the current number of metric points in the streaming buffer and its maximum capacity.",
    responses={401: {"description": "Missing or invalid X-API-Key."}},
)
def buffer_status(_: None = Security(require_key)) -> BufferStatus:
    with buffer._buffer_lock:
        return BufferStatus(size=len(buffer._metric_buffer), capacity=buffer.BUFFER_CAP)


@router.delete(
    "/api/metrics/buffer",
    status_code=204,
    summary="Clear the buffer",
    description="Discards all buffered metric points without running analysis.",
    responses={401: {"description": "Missing or invalid X-API-Key."}},
)
def clear_buffer(_: None = Security(require_key)) -> None:
    with buffer._buffer_lock:
        buffer._metric_buffer.clear()
    logger.info("INFRST001 buffer_cleared")


@router.post(
    "/api/metrics/analyze",
    response_model=deps.Report,
    summary="Analyze buffered metrics",
    description="""
Drains the streaming buffer and runs the full pipeline on the accumulated points.
The buffer is cleared atomically before analysis starts so new points can be
pushed immediately without waiting for the pipeline to finish.

Returns the same `Report` schema as **POST /api/analyze**.
""",
    response_description="Full structured report generated from buffered points.",
    responses={
        400: {"description": "Buffer is empty."},
        401: {"description": "Missing or invalid X-API-Key."},
        504: {"description": "Pipeline timed out."},
    },
)
async def analyze_buffer(
    request: Request,
    _: None = Security(require_key),
) -> deps.Report:
    with buffer._buffer_lock:
        if not buffer._metric_buffer:
            raise HTTPException(status_code=400, detail="Buffer is empty. Push points via POST /api/metrics first.")
        metrics = list(buffer._metric_buffer)
        buffer._metric_buffer.clear()

    logger.info("INFRST002 buffer_flush", extra={"points": len(metrics)})
    return await deps.run_pipeline(AnalyzeRequest(metrics=metrics))


@router.get(
    "/api/events",
    summary="Live report stream (SSE)",
    description="""
Server-Sent Events endpoint. The server pushes a new event each time a report
is generated (via `/api/analyze` or `/api/metrics/analyze`).

**Event format:**
```
id: <report_id>
data: <Report JSON>
```

A `heartbeat` comment is sent every `SSE_INTERVAL` seconds (default 5) to
keep the connection alive through proxies and load-balancers.

### Dashboard usage
The dashboard connects automatically when the **Live** toggle is enabled.

### External usage (curl)
```bash
curl -N -H "X-API-Key: your_key" http://localhost:8000/api/events
```
""",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE stream of Report objects."},
        401: {"description": "Missing or invalid X-API-Key."},
    },
)
async def sse_events(
    request: Request,
    _: None = Security(require_key),
) -> StreamingResponse:
    interval = int(os.getenv("SSE_INTERVAL", "5"))

    async def generate():
        last_id = None
        while True:
            if await request.is_disconnected():
                logger.info("INFRST003 sse_disconnect")
                break
            try:
                report = deps.store.latest() if deps.store else None
                if report and report.report_id != last_id:
                    last_id = report.report_id
                    yield f"id: {last_id}\ndata: {report.model_dump_json()}\n\n"
                else:
                    yield ": heartbeat\n\n"
            except Exception as exc:
                logger.warning("WRNRST000 sse_error", extra={"error": str(exc)})
                yield f": error {exc}\n\n"
            await asyncio.sleep(interval)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )
