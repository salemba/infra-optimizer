"""
Node 1 — Ingest

Validates that metrics arrived in the state. Currently a pass-through;
kept as a dedicated node so pre-processing (deduplication, schema migration,
source tagging) can be added here without touching downstream nodes.
"""
from __future__ import annotations

import logging

from app.state import State

logger = logging.getLogger(__name__)


def ingest(state: State) -> State:
    logger.info("INFING000 pipeline_ingest", extra={"count": len(state["metrics"])})
    return state
