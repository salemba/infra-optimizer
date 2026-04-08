"""
Shared pipeline state — the contract between all nodes.

Kept in its own module so any node can import it without pulling in
pipeline logic or heavy dependencies.
"""
from __future__ import annotations

from typing import TypedDict

from app.models import Anomaly, EnrichedPoint, MetricPoint, MetricStats, Recommendation


class State(TypedDict):
    metrics:               list[MetricPoint]
    enriched:              list[EnrichedPoint]
    statistics:            dict[str, MetricStats]
    anomalies:             list[Anomaly]
    recommendations:       list[Recommendation]
    recommendation_error:  str | None
    report:                dict
