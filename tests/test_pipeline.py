"""
Unit tests for the pipeline nodes.

Covers the algorithmic logic that is correctness-critical and
requires no LLM: enrichment, statistics, anomaly detection, escalation.
The _recommend node is not tested here because it requires a live LLM call;
integration tests should cover it with a mocked Anthropic client.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import (
    AnalysisWindow,
    Anomaly,
    EnrichedPoint,
    MetricPoint,
    MetricStats,
    Recommendation,
    ReportSummary,
    ServiceStatus,
)
from app.configuration import NUMERIC_METRICS, THRESHOLDS
from app.nodes.analyze import _p95, _severity, analyze as _analyze
from app.nodes.enrich import _stress_index, _trend, enrich as _enrich


# ── Fixtures ───────────────────────────────────────────────────────────────

def make_metric(
    cpu: float = 55.0,
    memory: float = 65.0,
    latency: float = 150.0,
    disk: float = 60.0,
    temp: float = 60.0,
    error_rate: float = 0.02,
    io_wait: float = 3.0,
    hour: int = 14,
    db: str = "online",
    gw: str = "online",
    cache: str = "online",
) -> MetricPoint:
    return MetricPoint(
        timestamp=datetime(2023, 10, 1, hour, 0, 0, tzinfo=timezone.utc),
        cpu_usage=cpu,
        memory_usage=memory,
        latency_ms=latency,
        disk_usage=disk,
        network_in_kbps=1000.0,
        network_out_kbps=1000.0,
        io_wait=io_wait,
        thread_count=150,
        active_connections=50,
        error_rate=error_rate,
        uptime_seconds=360000,
        temperature_celsius=temp,
        power_consumption_watts=250.0,
        service_status=ServiceStatus(database=db, api_gateway=gw, cache=cache),
    )


def make_enriched(
    timestamp: str = "2023-10-01T14:00:00+00:00",
    stress_index: float = 60.0,
    trend: dict | None = None,
    sustained: dict | None = None,
    time_context: str = "peak",
) -> EnrichedPoint:
    return EnrichedPoint(
        timestamp=timestamp,
        stress_index=stress_index,
        trend=trend or {f: "stable" for f in NUMERIC_METRICS},
        sustained=sustained or {f: False for f in NUMERIC_METRICS},
        time_context=time_context,
    )


def _base_state(metrics, enriched=None):
    return {
        "metrics":         metrics,
        "enriched":        enriched or [],
        "statistics":      {},
        "anomalies":       [],
        "recommendations": [],
        "report":          {},
    }


# ── _trend ─────────────────────────────────────────────────────────────────

class TestTrend:
    def test_rising(self):
        assert _trend([50.0, 65.0, 85.0]) == "rising"

    def test_falling(self):
        assert _trend([85.0, 65.0, 50.0]) == "falling"

    def test_stable_flat(self):
        assert _trend([60.0, 60.0, 60.0]) == "stable"

    def test_stable_small_delta(self):
        # delta within 5% dead-band of range
        assert _trend([60.0, 61.0, 60.5]) == "stable"

    def test_single_value(self):
        assert _trend([75.0]) == "stable"

    def test_two_values_rising(self):
        assert _trend([50.0, 90.0]) == "rising"


# ── _p95 ───────────────────────────────────────────────────────────────────

class TestP95:
    def test_known_value(self):
        values = list(range(1, 101))  # 1..100
        assert _p95(values) == 95

    def test_single_element(self):
        assert _p95([42.0]) == 42.0

    def test_all_same(self):
        assert _p95([7.0] * 10) == 7.0


# ── _severity ──────────────────────────────────────────────────────────────

class TestSeverity:
    def test_below_warning(self):
        assert _severity("cpu_usage", 74.9) is None

    def test_at_warning(self):
        assert _severity("cpu_usage", 75.0) == "warning"

    def test_above_warning_below_critical(self):
        assert _severity("cpu_usage", 85.0) == "warning"

    def test_at_critical(self):
        assert _severity("cpu_usage", 90.0) == "critical"

    def test_above_critical(self):
        assert _severity("cpu_usage", 95.0) == "critical"

    def test_error_rate_float_threshold(self):
        assert _severity("error_rate", 0.04) is None
        assert _severity("error_rate", 0.05) == "warning"
        assert _severity("error_rate", 0.10) == "critical"


# ── _stress_index ──────────────────────────────────────────────────────────

class TestStressIndex:
    def test_all_normal_is_low(self):
        m = make_metric(cpu=50, memory=60, latency=100, disk=50, temp=55,
                        error_rate=0.01, io_wait=2)
        assert _stress_index(m) < 70

    def test_all_critical_is_high(self):
        m = make_metric(
            cpu=THRESHOLDS["cpu_usage"]["critical"],
            memory=THRESHOLDS["memory_usage"]["critical"],
            latency=THRESHOLDS["latency_ms"]["critical"],
            disk=THRESHOLDS["disk_usage"]["critical"],
            temp=THRESHOLDS["temperature_celsius"]["critical"],
            error_rate=THRESHOLDS["error_rate"]["critical"],
            io_wait=THRESHOLDS["io_wait"]["critical"],
        )
        assert _stress_index(m) == pytest.approx(100.0)

    def test_capped_at_100(self):
        # values above critical should not push score above 100
        m = make_metric(cpu=999, memory=999, latency=9999)
        assert _stress_index(m) <= 100.0


# ── _enrich ────────────────────────────────────────────────────────────────

class TestEnrich:
    def test_time_context_peak(self):
        metrics = [make_metric(hour=10)]  # peak hour
        result  = _enrich(_base_state(metrics))
        assert result["enriched"][0].time_context == "peak"

    def test_time_context_offpeak(self):
        metrics = [make_metric(hour=3)]   # off-peak
        result  = _enrich(_base_state(metrics))
        assert result["enriched"][0].time_context == "off-peak"

    def test_sustained_requires_3_points(self):
        # 2 points above threshold — sustained must be False
        metrics = [make_metric(cpu=80), make_metric(cpu=80)]
        result  = _enrich(_base_state(metrics))
        assert result["enriched"][-1].sustained["cpu_usage"] is False

    def test_sustained_true_after_3_points(self):
        metrics = [make_metric(cpu=80), make_metric(cpu=80), make_metric(cpu=80)]
        result  = _enrich(_base_state(metrics))
        assert result["enriched"][-1].sustained["cpu_usage"] is True

    def test_sustained_false_if_one_point_normal(self):
        metrics = [make_metric(cpu=80), make_metric(cpu=50), make_metric(cpu=80)]
        result  = _enrich(_base_state(metrics))
        assert result["enriched"][-1].sustained["cpu_usage"] is False

    def test_trend_detected(self):
        metrics = [make_metric(cpu=50), make_metric(cpu=65), make_metric(cpu=82)]
        result  = _enrich(_base_state(metrics))
        assert result["enriched"][-1].trend["cpu_usage"] == "rising"

    def test_stress_index_in_enrichment(self):
        metrics = [make_metric(cpu=55)]
        result  = _enrich(_base_state(metrics))
        assert 0 <= result["enriched"][0].stress_index <= 100


# ── _analyze escalation ────────────────────────────────────────────────────

class TestAnalyze:
    def _run(self, metrics, enriched):
        state = _base_state(metrics, enriched)
        return _analyze(state)

    def test_no_anomaly_when_normal(self):
        metrics  = [make_metric()]
        enriched = [make_enriched()]
        result   = self._run(metrics, enriched)
        assert result["anomalies"] == []

    def test_warning_detected(self):
        metrics  = [make_metric(cpu=80)]   # above 75 warning
        enriched = [make_enriched()]
        result   = self._run(metrics, enriched)
        assert len(result["anomalies"]) == 1
        assert result["anomalies"][0].severity == "warning"

    def test_critical_detected(self):
        metrics  = [make_metric(cpu=92)]   # above 90 critical
        enriched = [make_enriched()]
        result   = self._run(metrics, enriched)
        assert result["anomalies"][0].severity == "critical"

    def test_escalation_sustained_offpeak(self):
        """Warning + sustained + off-peak → escalated to critical."""
        metrics  = [make_metric(cpu=80, hour=3)]
        enriched = [make_enriched(
            timestamp=metrics[0].timestamp.isoformat(),
            sustained={f: (f == "cpu_usage") for f in NUMERIC_METRICS},
            time_context="off-peak",
        )]
        result = self._run(metrics, enriched)
        cpu_hit = next(
            t for t in result["anomalies"][0].triggered_metrics
            if t.metric == "cpu_usage"
        )
        assert cpu_hit.severity == "critical"

    def test_escalation_rising_trend(self):
        """Warning + rising trend → escalated to critical."""
        metrics  = [make_metric(cpu=80, hour=10)]  # peak hour
        enriched = [make_enriched(
            timestamp=metrics[0].timestamp.isoformat(),
            trend={f: ("rising" if f == "cpu_usage" else "stable") for f in NUMERIC_METRICS},
            time_context="peak",
        )]
        result = self._run(metrics, enriched)
        cpu_hit = next(
            t for t in result["anomalies"][0].triggered_metrics
            if t.metric == "cpu_usage"
        )
        assert cpu_hit.severity == "critical"

    def test_no_escalation_sustained_peak(self):
        """Warning + sustained + peak → stays warning (escalation requires off-peak)."""
        metrics  = [make_metric(cpu=80, hour=10)]
        enriched = [make_enriched(
            timestamp=metrics[0].timestamp.isoformat(),
            sustained={f: (f == "cpu_usage") for f in NUMERIC_METRICS},
            time_context="peak",
        )]
        result = self._run(metrics, enriched)
        cpu_hit = next(
            t for t in result["anomalies"][0].triggered_metrics
            if t.metric == "cpu_usage"
        )
        assert cpu_hit.severity == "warning"

    def test_service_offline_is_critical(self):
        metrics  = [make_metric(db="offline")]
        enriched = [make_enriched(timestamp=metrics[0].timestamp.isoformat())]
        result   = self._run(metrics, enriched)
        assert result["anomalies"][0].severity == "critical"
        assert "database" in result["anomalies"][0].offline_services

    def test_service_degraded_is_warning(self):
        metrics  = [make_metric(gw="degraded")]
        enriched = [make_enriched(timestamp=metrics[0].timestamp.isoformat())]
        result   = self._run(metrics, enriched)
        assert result["anomalies"][0].severity == "warning"
        assert "api_gateway" in result["anomalies"][0].degraded_services

    def test_statistics_computed(self):
        metrics  = [make_metric(cpu=60), make_metric(cpu=80)]
        enriched = [make_enriched(), make_enriched()]
        result   = self._run(metrics, enriched)
        cpu_stats = result["statistics"]["cpu_usage"]
        assert cpu_stats.min == 60.0
        assert cpu_stats.max == 80.0
        assert cpu_stats.avg == 70.0
