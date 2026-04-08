"""
Streaming metric buffer — shared state for the /api/metrics* routes.

Thread-safe via _buffer_lock. Capacity is capped by BUFFER_SIZE (default 1000).
"""
from __future__ import annotations

import os
import threading

from app.models import MetricPoint

_metric_buffer: list[MetricPoint] = []
_buffer_lock   = threading.Lock()
BUFFER_CAP     = int(os.getenv("BUFFER_SIZE", "1000"))
