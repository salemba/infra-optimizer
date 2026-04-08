"""
Node 2 — Enrich

Derives contextual signals from the raw metric stream:
  - stress_index : composite 0–100 health score
  - trend        : rising / stable / falling per metric (3-point sliding window)
  - sustained    : True when a metric has been above warning threshold ≥ 3 consecutive points
  - time_context : peak vs off-peak based on wall-clock hour

Performance notes
-----------------
- deque(maxlen=3) gives O(1) append + eviction (was list.pop(0), O(n))
- All derived fields computed in a single pass over the metric list
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Literal

from app.configuration import NUMERIC_METRICS, PEAK_HOURS, STRESS_WEIGHTS, THRESHOLDS
from app.models import EnrichedPoint, MetricPoint
from app.state import State

logger = logging.getLogger(__name__)


def _trend(values: list[float]) -> Literal["rising", "stable", "falling"]:
    if len(values) < 2:
        return "stable"
    delta = values[-1] - values[0]
    band  = (max(values) - min(values)) * 0.05 if max(values) != min(values) else 1
    if delta >  band: return "rising"
    if delta < -band: return "falling"
    return "stable"


def _stress_index(m: MetricPoint) -> float:
    """Composite 0–100 score. Each metric normalised against its critical threshold."""
    score = 0.0
    for metric, weight in STRESS_WEIGHTS.items():
        normalised = min(getattr(m, metric) / THRESHOLDS[metric]["critical"] * 100, 100)
        score += normalised * weight
    return round(score, 1)


def enrich(state: State) -> State:
    metrics  = state["metrics"]
    enriched = []

    # deque(maxlen=3) — O(1) append and automatic eviction of oldest entry
    history: dict[str, deque] = {f: deque(maxlen=3) for f in NUMERIC_METRICS}

    for m in metrics:
        for f in NUMERIC_METRICS:
            history[f].append(getattr(m, f))

        trend = {f: _trend(list(history[f])) for f in NUMERIC_METRICS}
        sustained = {
            f: len(history[f]) >= 3 and all(v >= THRESHOLDS[f]["warning"] for v in history[f])
            for f in NUMERIC_METRICS
        }
        time_ctx: Literal["peak", "off-peak"] = (
            "peak" if m.timestamp.hour in PEAK_HOURS else "off-peak"
        )

        enriched.append(EnrichedPoint(
            timestamp=m.timestamp.isoformat(),
            stress_index=_stress_index(m),
            trend=trend,
            sustained=sustained,
            time_context=time_ctx,
        ))

    logger.info("INFENR000 pipeline_enrich", extra={"points": len(enriched)})
    return {**state, "enriched": enriched}
