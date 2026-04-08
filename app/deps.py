"""
Shared singletons — populated by the lifespan in api.py, consumed by routers.

Keeping these in a dedicated module breaks the circular-import that would arise
if routers imported directly from api.py.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import HTTPException

from app.graph import run as _graph_run
from app.models import AnalyzeRequest, Report
from app.store import DuckDBStore, ReportStore

logger = logging.getLogger(__name__)

# Set by lifespan in api.py
store: ReportStore | None = None
http_client: httpx.AsyncClient | None = None


async def run_pipeline(request_obj: AnalyzeRequest) -> Report:
    """
    Offload the blocking pipeline to a thread pool; raise HTTP 504 on timeout.

    When a DuckDBStore is active, also persists the raw MetricPoints so that
    GET /api/metrics/history can answer timeseries queries across multiple runs.
    """
    timeout = float(os.getenv("PIPELINE_TIMEOUT", "60"))
    loop = asyncio.get_event_loop()
    try:
        result: Report = await asyncio.wait_for(
            loop.run_in_executor(None, _graph_run, request_obj),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error("ERRDEP000 pipeline_timeout", extra={"timeout": timeout})
        raise HTTPException(status_code=504, detail="Pipeline timed out.")

    # Persist raw metrics when the store supports it (DuckDB only)
    if isinstance(store, DuckDBStore):
        store.save_with_metrics(result, request_obj.metrics)
    elif store is not None:
        store.save(result)

    return result
