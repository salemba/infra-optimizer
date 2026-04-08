"""
Ops routes — no business logic, no auth on /metrics (Prometheus scrapers can't send headers).

  GET /health     liveness probe
  GET /           dashboard
  GET /metrics    Prometheus scrape target
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Security
from fastapi.responses import FileResponse, PlainTextResponse

from app import buffer, deps
from app.security import require_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

_DASHBOARD = Path(__file__).parent.parent.parent / "dashboard" / "index.html"


@router.get(
    "/health",
    summary="Liveness probe",
    description="Returns `{\"status\": \"ok\"}`. Used by Docker healthcheck, k8s, and reverse proxies.",
    response_description="Service is up.",
)
def health():
    return {"status": "ok"}


@router.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(_DASHBOARD)


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus scrape endpoint",
    description="""
Standard Prometheus text exposition format. Add as a scrape target:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: infra-optimizer
    static_configs:
      - targets: ['infra-optimizer:8000']
```

**No authentication** on this route by design — Prometheus scrapers cannot send
custom headers. Restrict access at the network level (firewall / VPN).

Disable by setting `PROMETHEUS_ENABLED=false`.
""",
    response_description="Prometheus text metrics.",
    responses={404: {"description": "Prometheus endpoint is disabled."}},
)
def prometheus_metrics() -> str:
    if os.getenv("PROMETHEUS_ENABLED", "true").lower() != "true":
        raise HTTPException(status_code=404)

    report = deps.store.latest() if deps.store else None
    if report is None:
        return "# No data yet\n"

    lines: list[str] = []

    def gauge(name: str, value: float | int, help_: str = "") -> None:
        if help_:
            lines.append(f"# HELP infra_{name} {help_}")
        lines.append(f"# TYPE infra_{name} gauge")
        lines.append(f"infra_{name} {value}")

    s = report.summary
    gauge("anomaly_count",     s.anomaly_count,     "Total anomalies in last report")
    gauge("critical_count",    s.critical_count,    "Critical anomalies in last report")
    gauge("warning_count",     s.warning_count,     "Warning anomalies in last report")
    gauge("avg_stress_index",  s.avg_stress_index,  "Average composite stress index 0-100")
    gauge("peak_stress_index", s.peak_stress_index, "Peak composite stress index 0-100")
    gauge("health_status",
          {"healthy": 0, "warning": 1, "critical": 2}.get(s.overall_health, -1),
          "Overall health: 0=healthy 1=warning 2=critical")
    with buffer._buffer_lock:
        gauge("buffer_size", len(buffer._metric_buffer), "Current streaming buffer size")

    for metric, stats in report.statistics.items():
        for stat, val in stats.model_dump().items():
            gauge(f"{metric}_{stat}", val)

    return "\n".join(lines) + "\n"
