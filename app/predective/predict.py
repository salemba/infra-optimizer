import json
import logging
import os
from datetime import timedelta
from statistics import mean, stdev

import anthropic

from app.configuration import THRESHOLDS
from app.models import MetricPoint, PredictionResult, Recommendation

logger = logging.getLogger(__name__)

# Lazy singleton — instantiated on the first LLM call, not at import time.
# This prevents an APIKeyNotFoundError crash at startup when ANTHROPIC_API_KEY
# is not yet set (CI pipelines, local dev without .env, uvicorn cold-start).
_llm_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _llm_client
    if _llm_client is None:
        _llm_client = anthropic.Anthropic()
    return _llm_client

# The LLM is asked to reason about the historical trend "as if" applying
# FBProphet-style decomposition (trend + seasonality), then project one step
# ahead and classify it against the provided thresholds.
# This is a prompt-engineering approximation — not a real FBProphet run.
_SYSTEM = """
You are an infrastructure expert specialised in time-series forecasting.

The user prompt contains:
- A compact statistical summary of the last N metric points (window).
- A compact CPU time-series array (all window values) so you can detect
  recurring spike patterns / seasonality.
- The exact target_timestamp you must predict (T + 30 min).

Your task:
1. Analyse trend, seasonality, and volatility (stdev) in the data.
2. Predict each metric value at target_timestamp.
3. Compare the predicted cpu_usage (most critical) against the thresholds.
4. Determine severity: "critical" > critical threshold, "warning" > warning
   threshold, otherwise "healthy".
5. If severity is "warning" or "critical", generate recommendations.

Always return a single JSON object — even for "healthy" predictions:
{
  "target_timestamp": "<ISO 8601>",
  "severity": "healthy|warning|critical",
  "predicted_metrics": {
    "cpu_usage": <float>,
    "memory_usage": <float>,
    "latency_ms": <float>,
    "disk_usage": <float>,
    "temperature_celsius": <float>,
    "error_rate": <float>,
    "io_wait": <float>
  },
  "recommendations": []   // empty array when severity == "healthy"
}
Return ONLY the JSON object — no prose, no markdown fences.
"""



_PREDICT_METRICS = [
    "cpu_usage", "memory_usage", "latency_ms", "disk_usage",
    "temperature_celsius", "error_rate", "io_wait",
]

_DEFAULT_WINDOW = 12


def build_prompt(metrics: list[MetricPoint], window: int | None = None) -> str:
    """
    Build a compact prediction prompt from the tail of a metric series.

    Instead of serialising every raw field of every data point (excessive tokens),
    this function:
      1. Keeps only the last `window` points (default 12 = 6 h at 30-min cadence).
      2. For each relevant metric, computes: last value, mean, and linear trend
         (last - first in the window, i.e. drift over the window).
      3. Attaches the configured warning/critical thresholds so the LLM can
         classify the predicted value without needing the full config.
      4. Includes only the last 3 raw timestamps so the LLM understands the
         cadence without receiving the full series.

    This reduces typical prompt size from ~50 kB (full dataset) to < 1 kB.
    """
    w = window or int(os.getenv("PREDICT_WINDOW", str(_DEFAULT_WINDOW)))
    window_points = metrics[-w:]

    metric_summaries = {}
    for m in _PREDICT_METRICS:
        values = [float(getattr(pt, m)) for pt in window_points]
        if not values:
            continue
        metric_summaries[m] = {
            "last":      round(values[-1], 3),
            "mean":      round(mean(values), 3),
            "drift":     round(values[-1] - values[0], 3),  # positive = rising
            "thresholds": THRESHOLDS.get(m, {}),
        }

    recent_timestamps = [pt.timestamp.isoformat() for pt in window_points[-3:]]

    payload = {
        "window_size":        len(window_points),
        "recent_timestamps":  recent_timestamps,
        "metric_summaries":   metric_summaries,
    }
    return json.dumps(payload, default=str)


def predict(prompt: str) -> PredictionResult | None:
    """
    Call the LLM with a compressed metric history prompt and return a
    PredictionResult for the next 30-minute data point.

    Returns None on LLM error so callers can distinguish "healthy" from failure.
    """
    client = _get_client()
    raw = ""
    try:
        msg = client.messages.create(
            model=os.getenv("LLM_MODEL", "claude-opus-4-6"),
            max_tokens=int(os.getenv("MAX_TOKENS", "2048")),
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip() if msg.content else "{}"
        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1].removeprefix("json").strip()
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        parsed = json.loads(raw)
        result = PredictionResult(
            target_timestamp  = parsed["target_timestamp"],
            severity          = parsed["severity"],
            predicted_metrics = parsed["predicted_metrics"],
            recommendations   = [Recommendation(**r) for r in parsed.get("recommendations", [])],
        )
        logger.info("INFPRD000 predict_ok", extra={
            "severity": result.severity,
            "target":   result.target_timestamp,
            "recs":     len(result.recommendations),
        })
        return result
    except Exception as exc:
        logger.warning("WRNPRD000 predict_failed",
                       extra={"error": f"{type(exc).__name__}: {exc}",
                              "raw_preview": raw[:400]})
        return None



