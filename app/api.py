"""
FastAPI application — factory, middleware, and lifespan only.

Routes live in app/routers/:
  analysis.py   POST /api/analyze, GET /api/report/latest, GET /api/reports
  streaming.py  POST /api/metrics*, GET /api/events
  ops.py        GET /health, GET /, GET /metrics

Shared infrastructure:
  app/deps.py       store + http_client singletons
  app/security.py   auth dependency + SSRF guard + rate limiter
  app/buffer.py     streaming buffer state
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from app import deps
from app.routers import analysis, feedback, ops, streaming
from app.security import limiter
from app.store import make_store

logger = logging.getLogger(__name__)

# ── Lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    deps.store       = make_store(os.getenv("STORE_DSN"))
    deps.http_client = httpx.AsyncClient(timeout=5)
    if os.getenv("CORS_ORIGINS", "*") == "*" and os.getenv("ENV", "prod") != "dev":
        logger.warning("WRNAPI000 cors_open", extra={
            "msg": "CORS_ORIGINS=* in non-dev mode — restrict origins in production"
        })
    logger.info("INFAPI000 startup", extra={"store": os.getenv("STORE_DSN", "memory")})
    yield
    await deps.http_client.aclose()
    logger.info("INFAPI001 shutdown")


# ── App ────────────────────────────────────────────────────────────────────

_TAGS = [
    {
        "name": "analysis",
        "description": (
            "Core analysis pipeline. **POST /api/analyze** runs the full "
            "ingest → enrich → analyze → recommend → report pipeline on a batch "
            "of metric points."
        ),
    },
    {
        "name": "streaming",
        "description": (
            "Real-time metric ingestion. Push individual metric points to the "
            "in-memory buffer, then trigger analysis on demand. "
            "Subscribe to **GET /api/events** (SSE) to receive new reports "
            "as they are generated."
        ),
    },
    {
        "name": "feedback",
        "description": (
            "Recommendation outcome tracking. Submit per-recommendation feedback "
            "after acting on a report, then query **GET /api/feedback/summary** "
            "to see which categories actually resolve issues."
        ),
    },
    {
        "name": "ops",
        "description": (
            "Operational endpoints: liveness probe, Prometheus scrape target. "
            "Place `/metrics` behind a firewall — Prometheus scrapers cannot "
            "send auth headers."
        ),
    },
]

app = FastAPI(
    title="InfraOptimizer",
    description="""
On-premise infrastructure anomaly detection and recommendation engine powered by LLM.

## Workflows

### Batch (file upload / CI pipeline)
```
POST /api/analyze   →   GET /api/report/latest
```

### Streaming (monitoring agent / real-time)
```
POST /api/metrics  (repeat)
POST /api/metrics/analyze
GET  /api/events   (SSE — receives the report when ready)
```

### Grafana / Prometheus integration
```
GET /metrics   →   Prometheus scrape target
```

## Authentication
Set `API_KEY` in your `.env` file. All `/api/*` routes then require the
`X-API-Key` header. Leave `API_KEY` unset to disable auth (private network only).
""",
    version="1.0.0",
    openapi_tags=_TAGS,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(GZipMiddleware, minimum_size=1024)

_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["X-API-Key", "Content-Type"],
)

_static_dir = Path(__file__).parent.parent / "dashboard" / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# ── Routers ────────────────────────────────────────────────────────────────

app.include_router(ops.router)
app.include_router(analysis.router)
app.include_router(streaming.router)
app.include_router(feedback.router)
