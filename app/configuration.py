"""
Runtime configuration — thresholds, weights, and per-invocation LangGraph config.

Operators can tune detection by editing thresholds.json and restarting the server.
No code change or redeploy needed.

Per-invocation overrides (e.g. a different LLM model for one request) are handled
via the LangGraph RunnableConfig mechanism: pass {"configurable": {"llm_model": "..."}}
to graph.invoke() and the recommend node will pick it up via Configuration.from_runnable_config().
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "cpu_usage":           {"warning": 75,   "critical": 90},
    "memory_usage":        {"warning": 75,   "critical": 88},
    "latency_ms":          {"warning": 200,  "critical": 300},
    "disk_usage":          {"warning": 75,   "critical": 88},
    "temperature_celsius": {"warning": 70,   "critical": 82},
    "error_rate":          {"warning": 0.05, "critical": 0.10},
    "io_wait":             {"warning": 5,    "critical": 10},
}


def load_thresholds() -> dict[str, dict[str, float]]:
    path = Path(os.getenv("THRESHOLDS_FILE", "thresholds.json"))
    if path.exists():
        with open(path) as f:
            loaded = json.load(f)
        logger.info("INFCFG000 thresholds_loaded", extra={"path": str(path)})
        return loaded
    logger.info("INFCFG001 thresholds_default", extra={"reason": "file not found, using defaults"})
    return _DEFAULT_THRESHOLDS


THRESHOLDS: dict[str, dict[str, float]] = load_thresholds()
NUMERIC_METRICS: list[str] = list(THRESHOLDS.keys())

# Weights for the composite stress index — must sum to 1.0
STRESS_WEIGHTS: dict[str, float] = {
    "cpu_usage":           0.25,
    "memory_usage":        0.20,
    "latency_ms":          0.20,
    "disk_usage":          0.15,
    "temperature_celsius": 0.10,
    "error_rate":          0.05,
    "io_wait":             0.05,
}
assert abs(sum(STRESS_WEIGHTS.values()) - 1.0) < 1e-9, "Stress weights must sum to 1.0"

PEAK_HOURS: set[int] = set(range(8, 12)) | set(range(14, 19))


@dataclass(kw_only=True)
class Configuration:
    """
    Per-invocation LangGraph configuration.

    Usage — override the LLM model for a single graph.invoke() call:
        graph.invoke(state, config={"configurable": {"llm_model": "claude-haiku-4-5-20251001"}})
    """
    llm_model:  str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-opus-4-6"))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("MAX_TOKENS", "4096")))

    @classmethod
    def from_runnable_config(cls, config: dict | None = None) -> "Configuration":
        """Extract only the fields this dataclass knows about from a RunnableConfig dict."""
        configurable = (config or {}).get("configurable", {})
        known = {k: v for k, v in configurable.items() if k in cls.__dataclass_fields__}
        return cls(**known)
