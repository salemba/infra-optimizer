"""
Node 5 — Report

Assembles the final structured Report from all upstream node outputs.
This is the only place where typed pipeline models are serialised to dict
(via model_dump()) for the state boundary.
"""
from __future__ import annotations

import logging
import statistics
import uuid
from datetime import datetime, timezone
from typing import Literal

from app.models import AnalysisWindow, Anomaly, Report, ReportSummary
from app.state import State

logger = logging.getLogger(__name__)


def build_report(state: State) -> State:
    metrics, anomalies, enriched = state["metrics"], state["anomalies"], state["enriched"]

    def _health(a: list[Anomaly]) -> Literal["healthy", "warning", "critical"]:
        if any(x.severity == "critical" for x in a): return "critical"
        if any(x.severity == "warning"  for x in a): return "warning"
        return "healthy"

    avg_stress  = round(statistics.mean(e.stress_index for e in enriched), 1) if enriched else 0.0
    peak_stress = round(max((e.stress_index for e in enriched), default=0.0), 1)

    report = Report(
        report_id=str(uuid.uuid4()),
        generated_at=datetime.now(timezone.utc).isoformat(),
        analysis_window=AnalysisWindow(
            start=metrics[0].timestamp.isoformat(),
            end=metrics[-1].timestamp.isoformat(),
            total_data_points=len(metrics),
        ),
        summary=ReportSummary(
            overall_health=_health(anomalies),
            anomaly_count=len(anomalies),
            critical_count=sum(1 for a in anomalies if a.severity == "critical"),
            warning_count=sum(1 for a in anomalies if a.severity == "warning"),
            avg_stress_index=avg_stress,
            peak_stress_index=peak_stress,
            recommendation_error=state.get("recommendation_error"),
        ),
        statistics=state["statistics"],
        enrichment=enriched,
        anomalies=anomalies,
        recommendations=state["recommendations"],
    )

    logger.info("INFRPT000 pipeline_report", extra={
        "report_id": report.report_id,
        "health":    report.summary.overall_health,
    })
    return {**state, "report": report.model_dump()}
