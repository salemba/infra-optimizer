"""
Node 3 — Analyze

Detects anomalies from enriched metric points:
  - Threshold breach detection (warning / critical) per metric
  - Severity escalation rules:
      * warning + sustained off-peak  → critical
      * warning + rising trend        → critical
  - Service status tracking (degraded / offline)

Performance note
----------------
Single-pass accumulation for statistics (was 4 generator passes × 7 metrics = 28 traversals).
"""
from __future__ import annotations

import logging

from app.configuration import NUMERIC_METRICS, THRESHOLDS
from app.models import Anomaly, MetricStats, TriggeredMetric
from app.state import State

logger = logging.getLogger(__name__)


def _severity(metric: str, value: float) -> str | None:
    t = THRESHOLDS[metric]
    if value >= t["critical"]: return "critical"
    if value >= t["warning"]:  return "warning"
    return None


def _p95(values: list[float]) -> float:
    s = sorted(values)
    return s[min(int(0.95 * len(s)), len(s) - 1)]


def analyze(state: State) -> State:
    metrics  = state["metrics"]
    enriched = state["enriched"]

    # Single-pass accumulation — avoids repeated full-list traversals
    accum: dict[str, dict] = {
        f: {"lo": float("inf"), "hi": float("-inf"), "total": 0.0, "vals": []}
        for f in NUMERIC_METRICS
    }
    for m in metrics:
        for f in NUMERIC_METRICS:
            v = getattr(m, f)
            a = accum[f]
            a["total"] += v
            if v < a["lo"]: a["lo"] = v
            if v > a["hi"]: a["hi"] = v
            a["vals"].append(v)

    n = len(metrics)
    stats: dict[str, MetricStats] = {
        f: MetricStats(
            min=round(a["lo"], 3),
            max=round(a["hi"], 3),
            avg=round(a["total"] / n, 3),
            p95=round(_p95(a["vals"]), 3),
        )
        for f, a in accum.items()
    }

    anomalies: list[Anomaly] = []
    for m, e in zip(metrics, enriched):
        triggered, top_sev = [], None

        for f in NUMERIC_METRICS:
            base_sev = _severity(f, getattr(m, f))
            if not base_sev:
                continue

            # Escalation: sustained off-peak anomaly or rising trend on a warning metric
            effective_sev = base_sev
            if base_sev == "warning":
                if e.sustained.get(f) and e.time_context == "off-peak":
                    effective_sev = "critical"
                elif e.trend.get(f) == "rising":
                    effective_sev = "critical"

            triggered.append(TriggeredMetric(
                metric=f, value=getattr(m, f),
                threshold=THRESHOLDS[f][effective_sev],
                severity=effective_sev,
            ))
            if effective_sev == "critical" or top_sev is None:
                top_sev = effective_sev

        degraded, offline = [], []
        for svc, status in m.service_status.model_dump().items():
            if status == "degraded":
                degraded.append(svc)
                top_sev = top_sev or "warning"
            elif status == "offline":
                offline.append(svc)
                top_sev = "critical"

        if triggered or degraded or offline:
            anomalies.append(Anomaly(
                timestamp=m.timestamp.isoformat(),
                severity=top_sev,
                triggered_metrics=triggered,
                degraded_services=degraded,
                offline_services=offline,
                stress_index=e.stress_index,
                trend=e.trend,
                sustained=e.sustained,
                time_context=e.time_context,
            ))

    logger.info("INFANL000 pipeline_analyze", extra={"anomalies": len(anomalies)})
    return {**state, "statistics": stats, "anomalies": anomalies}
