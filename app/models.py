"""
Pydantic models — the shared contract between all layers.

Kept deliberately thin: no methods, no business logic.
Any layer can import these without pulling in pipeline or API dependencies.

M2 fix: summary and analysis_window are now typed models (was bare dict),
        so field-name typos are caught at development time, not runtime.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ── Input ──────────────────────────────────────────────────────────────────

class ServiceStatus(BaseModel):
    database:    Literal["online", "degraded", "offline"]
    api_gateway: Literal["online", "degraded", "offline"]
    cache:       Literal["online", "degraded", "offline"]


class MetricPoint(BaseModel):
    timestamp:               datetime
    host:                    str = "default"   # server/node identifier for multi-host deployments
    cpu_usage:               Annotated[float, Field(ge=0, le=100)]
    memory_usage:            Annotated[float, Field(ge=0, le=100)]
    latency_ms:              Annotated[float, Field(ge=0)]
    disk_usage:              Annotated[float, Field(ge=0, le=100)]
    network_in_kbps:         Annotated[float, Field(ge=0)]
    network_out_kbps:        Annotated[float, Field(ge=0)]
    io_wait:                 Annotated[float, Field(ge=0)]
    thread_count:            Annotated[int,   Field(ge=0)]
    active_connections:      Annotated[int,   Field(ge=0)]
    error_rate:              Annotated[float, Field(ge=0, le=1)]
    uptime_seconds:          Annotated[int,   Field(ge=0)]
    temperature_celsius:     Annotated[float, Field(ge=0)]
    power_consumption_watts: Annotated[float, Field(ge=0)]
    service_status:          ServiceStatus


class AnalyzeRequest(BaseModel):
    metrics:  Annotated[list[MetricPoint], Field(min_length=1)]
    # Per-request webhook overrides (e.g. alert a specific Slack channel for this run)
    webhooks: list[str] = []


class PredictRequest (BaseModel):
    metrics: Annotated [list[MetricPoint], Field(min_length=1)]
    
    webhooks: list[str] = []


# ── Output ─────────────────────────────────────────────────────────────────

class TriggeredMetric(BaseModel):
    metric:    str
    value:     float
    threshold: float
    severity:  Literal["warning", "critical"]


class EnrichedPoint(BaseModel):
    """
    Derived signals computed from the raw metric window.
    Carried alongside each MetricPoint through the pipeline so the analysis node
    can escalate sustained off-peak anomalies without duplicating enrichment logic.
    """
    timestamp:    str
    stress_index: float                  # 0–100 composite health score
    trend:        dict[str, str]         # metric → "rising" | "stable" | "falling"
    sustained:    dict[str, bool]        # metric → True if above warning threshold ≥ 3 pts
    time_context: Literal["peak", "off-peak"]


class Anomaly(BaseModel):
    timestamp:         str
    severity:          Literal["warning", "critical"]
    triggered_metrics: list[TriggeredMetric]
    degraded_services: list[str]
    offline_services:  list[str]
    # Enrichment context — gives the LLM richer material for recommendations
    stress_index:      float
    trend:             dict[str, str]
    sustained:         dict[str, bool]
    time_context:      str


class Recommendation(BaseModel):
    priority:    Literal["high", "medium", "low"]
    category:    str
    title:       str
    description: str
    actions:     list[str]


class MetricStats(BaseModel):
    min: float
    max: float
    avg: float
    p95: float


# M2 fix: typed models replacing bare dict ─────────────────────────────────

class AnalysisWindow(BaseModel):
    start:             str
    end:               str
    total_data_points: int


class ReportSummary(BaseModel):
    overall_health:       Literal["healthy", "warning", "critical"]
    anomaly_count:        int
    critical_count:       int
    warning_count:        int
    avg_stress_index:     float
    peak_stress_index:    float
    recommendation_error: str | None = None  # set when the LLM call fails


class BufferStatus(BaseModel):
    """Current state of the streaming metric buffer."""
    size:     int
    capacity: int


# ── Prediction ─────────────────────────────────────────────────────────────

class PredictionResult(BaseModel):
    """Single-point forecast returned by the predict node."""
    target_timestamp:  str
    severity:          Literal["healthy", "warning", "critical"]
    predicted_metrics: dict[str, float]   # metric → predicted value
    recommendations:   list[Recommendation]  # empty when severity == "healthy"


# ── Feedback ───────────────────────────────────────────────────────────────

class FeedbackStatus(str, Enum):
    resolved     = "resolved"      # followed the recommendation; problem went away
    partial      = "partial"       # helped but did not fully fix it
    not_relevant = "not_relevant"  # recommendation did not apply to this situation
    not_tried    = "not_tried"     # acknowledged but not acted on yet


class RecommendationFeedback(BaseModel):
    rec_index:    int
    status:       FeedbackStatus
    note:         str | None = None
    # Denormalized at write time for aggregation queries
    category:     str | None = None
    priority:     str | None = None
    title:        str | None = None
    submitted_at: str | None = None   # ISO timestamp, set server-side


class ReportFeedback(BaseModel):
    report_id:    str
    items:        list[RecommendationFeedback]
    submitted_at: str


class CategoryStats(BaseModel):
    """Resolution stats for one category or priority bucket."""
    label:           str    # category name or priority level
    total:           int
    resolved:        int
    partial:         int
    not_relevant:    int
    not_tried:       int
    resolution_rate: float  # (resolved + partial) / total, 0.0 when total == 0


class FeedbackSummary(BaseModel):
    total_feedback:          int
    overall_resolution_rate: float
    by_category:             list[CategoryStats]
    by_priority:             list[CategoryStats]


class Report(BaseModel):
    report_id:       str
    generated_at:    str
    analysis_window: AnalysisWindow           # was dict
    summary:         ReportSummary            # was dict
    statistics:      dict[str, MetricStats]
    enrichment:      list[EnrichedPoint]
    anomalies:       list[Anomaly]
    recommendations: list[Recommendation]
