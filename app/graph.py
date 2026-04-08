"""
Graph assembly — single source of truth for the pipeline topology.

The compiled `graph` symbol is exported for:
  - langgraph.json  (langgraph dev / Platform deployment)
  - app/api.py      (HTTP handler calls run())
  - CLI / tests     (direct run() calls)

Adding a new node: add_node() + add_edge() here; no other file needs changing.
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.models import AnalyzeRequest, Report
from app.nodes.analyze import analyze
from app.nodes.enrich import enrich
from app.nodes.ingest import ingest
from app.nodes.recommend import recommend
from app.nodes.report import build_report
from app.state import State


def _build() -> StateGraph:
    g = StateGraph(State)
    g.add_node("ingest",    ingest)
    g.add_node("enrich",    enrich)
    g.add_node("analyze",   analyze)
    g.add_node("recommend", recommend)
    g.add_node("report",    build_report)
    g.set_entry_point("ingest")
    g.add_edge("ingest",    "enrich")
    g.add_edge("enrich",    "analyze")
    g.add_edge("analyze",   "recommend")
    g.add_edge("recommend", "report")
    g.add_edge("report",    END)
    return g.compile()


# Exported for langgraph.json — must be module-level
graph = _build()


def run(request: AnalyzeRequest) -> Report:
    """Single public entry point — used by the API and the CLI alike."""
    final = graph.invoke({
        "metrics":              request.metrics,
        "enriched":             [],
        "statistics":           {},
        "anomalies":            [],
        "recommendations":      [],
        "recommendation_error": None,
        "report":               {},
    })
    return Report.model_validate(final["report"])
