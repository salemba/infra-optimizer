"""
Node 4 — Recommend

Calls the Anthropic Claude API with a summary of detected anomalies and returns
structured remediation recommendations.

Design decisions
----------------
- Lazy client singleton: the Anthropic client is created on the first LLM call,
  not at import time. This prevents an import-time crash when ANTHROPIC_API_KEY
  is absent (CI, local dev without .env, uvicorn cold-start).
- Per-invocation model override via LangGraph RunnableConfig + Configuration dataclass.
- The entire LLM call AND response parsing are wrapped in a single try/except so that
  any failure (missing API key, network error, rate limit, malformed JSON) is captured
  in recommendation_error rather than silently returning empty recommendations.
"""
from __future__ import annotations

import json
import logging
import os

import anthropic
from langgraph.types import RunnableConfig

from app.configuration import Configuration
from app.models import Recommendation
from app.state import State

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

_SYSTEM = """
Tu es un expert infrastructure.
À partir du résumé d'anomalies enrichi fourni, retourne UNIQUEMENT un tableau JSON
de recommandations sans texte autour, chaque objet ayant :
{
  "priority": "high|medium|low",
  "category": "resource_scaling|load_balancing|service_recovery|capacity_planning|monitoring",
  "title": "...",
  "description": "...",
  "actions": ["..."]
}
Tiens compte des tendances (rising/falling), des anomalies soutenues et du contexte
horaire (peak vs off-peak) pour calibrer la priorité et les actions proposées.
"""


def recommend(state: State, config: RunnableConfig = None) -> State:
    anomalies = state["anomalies"]
    if not anomalies:
        return {**state, "recommendations": [], "recommendation_error": None}

    cfg = Configuration.from_runnable_config(config)

    critical = [a for a in anomalies if a.severity == "critical"]
    warning  = [a for a in anomalies if a.severity == "warning"]

    prompt = json.dumps({
        "critical_count":    len(critical),
        "warning_count":     len(warning),
        "critical_examples": [a.model_dump() for a in critical[:3]],
        "warning_examples":  [a.model_dump() for a in warning[:3]],
        "statistics":        {k: v.model_dump() for k, v in state["statistics"].items()},
    }, default=str)

    recs: list[Recommendation] = []
    error: str | None = None

    try:
        msg = _get_client().messages.create(
            model=cfg.llm_model,
            max_tokens=cfg.max_tokens,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip() if msg.content else ""
        logger.debug(
            "DBGREC000 llm_raw_response",
            extra={"model": cfg.llm_model, "stop_reason": msg.stop_reason,
                   "usage": msg.usage.model_dump() if msg.usage else None,
                   "raw_preview": raw[:500]},
        )
        # Strip opening ```json / ``` fence AND closing ``` fence
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1].lstrip("json").strip()
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        recs = [Recommendation(**r) for r in json.loads(raw)]
        logger.info("INFREC000 pipeline_recommend", extra={"count": len(recs)})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "WRNREC000 recommendation_failed",
            extra={"error": error, "raw_preview": locals().get("raw", "")[:500]},
        )

    return {**state, "recommendations": recs, "recommendation_error": error}
