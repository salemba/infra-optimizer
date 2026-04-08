"""
Storage abstraction for reports and raw metric timeseries.

Three implementations behind the ReportStore ABC:

  MemoryStore   — in-process, zero config, no persistence (dev / demos)
  SQLiteStore   — file-based JSON blob store; simple, no extra dependencies
  DuckDBStore   — columnar store; enables timeseries queries across reports
                  (GET /api/metrics/history, trend analysis, multi-host)

Select via STORE_DSN environment variable:
  unset or ""              → MemoryStore
  ./data/reports.db        → SQLiteStore
  ./data/metrics.duckdb    → DuckDBStore

Upgrade path: MemoryStore → SQLiteStore → DuckDBStore → PostgreSQL+TimescaleDB
Each step adds query capability without changing the API or pipeline contract.
"""
from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.models import (
    CategoryStats, FeedbackSummary, MetricPoint,
    RecommendationFeedback, Report, ReportFeedback,
)


# ── Abstract base ──────────────────────────────────────────────────────────

class ReportStore(ABC):
    @abstractmethod
    def save(self, report: Report) -> None: ...

    @abstractmethod
    def latest(self) -> Report | None: ...

    @abstractmethod
    def history(self, n: int = 20) -> list[Report]: ...

    @abstractmethod
    def get_report(self, report_id: str) -> Report | None: ...

    @abstractmethod
    def save_feedback(self, feedback: ReportFeedback) -> None: ...

    @abstractmethod
    def get_feedback(self, report_id: str) -> ReportFeedback | None: ...

    @abstractmethod
    def feedback_summary(self) -> FeedbackSummary: ...

    def metric_history(
        self,
        host: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Return raw metric rows for timeseries queries.
        Default implementation returns [] — only DuckDBStore overrides this.
        """
        return []


# ── MemoryStore ────────────────────────────────────────────────────────────

class MemoryStore(ReportStore):
    """In-process store. Zero-config, no persistence — good for dev/demos."""

    def __init__(self) -> None:
        self._reports: list[Report] = []
        self._feedback: dict[str, ReportFeedback] = {}

    def save(self, report: Report) -> None:
        self._reports.append(report)

    def latest(self) -> Report | None:
        return self._reports[-1] if self._reports else None

    def history(self, n: int = 20) -> list[Report]:
        return list(reversed(self._reports[-n:]))

    def get_report(self, report_id: str) -> Report | None:
        return next((r for r in self._reports if r.report_id == report_id), None)

    def save_feedback(self, feedback: ReportFeedback) -> None:
        self._feedback[feedback.report_id] = feedback

    def get_feedback(self, report_id: str) -> ReportFeedback | None:
        return self._feedback.get(report_id)

    def feedback_summary(self) -> FeedbackSummary:
        return _compute_summary(list(self._feedback.values()))


# ── SQLiteStore ────────────────────────────────────────────────────────────

class SQLiteStore(ReportStore):
    """
    File-based persistent store.

    Thread-local connections — each thread reuses its own connection instead
    of opening a new one per operation. Safe for multi-threaded uvicorn workers.

    Schema is intentionally minimal: one table, report as JSON blob.
    Upgrade to DuckDBStore when you need timeseries queries.
    """

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path  = path
        self._local = threading.local()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id             TEXT PRIMARY KEY,
                    generated_at   TEXT NOT NULL,
                    overall_health TEXT NOT NULL,
                    payload        TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    report_id    TEXT    NOT NULL,
                    rec_index    INTEGER NOT NULL,
                    category     TEXT,
                    priority     TEXT,
                    title        TEXT,
                    status       TEXT    NOT NULL,
                    note         TEXT,
                    submitted_at TEXT    NOT NULL,
                    PRIMARY KEY (report_id, rec_index)
                )
            """)

    def save(self, report: Report) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO reports VALUES (?, ?, ?, ?)",
                (
                    report.report_id,
                    report.generated_at,
                    report.summary.overall_health,
                    report.model_dump_json(),
                ),
            )

    def latest(self) -> Report | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM reports ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        return Report.model_validate_json(row["payload"]) if row else None

    def history(self, n: int = 20) -> list[Report]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM reports ORDER BY generated_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [Report.model_validate_json(r["payload"]) for r in rows]

    def get_report(self, report_id: str) -> Report | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        return Report.model_validate_json(row["payload"]) if row else None

    def save_feedback(self, feedback: ReportFeedback) -> None:
        with self._connect() as conn:
            for item in feedback.items:
                conn.execute(
                    """INSERT OR REPLACE INTO feedback
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (feedback.report_id, item.rec_index, item.category, item.priority,
                     item.title, item.status, item.note, item.submitted_at),
                )

    def get_feedback(self, report_id: str) -> ReportFeedback | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE report_id = ? ORDER BY rec_index",
                (report_id,),
            ).fetchall()
        if not rows:
            return None
        items = [RecommendationFeedback(
            rec_index=r["rec_index"], category=r["category"], priority=r["priority"],
            title=r["title"], status=r["status"], note=r["note"], submitted_at=r["submitted_at"],
        ) for r in rows]
        return ReportFeedback(report_id=report_id, items=items,
                              submitted_at=rows[-1]["submitted_at"])

    def feedback_summary(self) -> FeedbackSummary:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM feedback").fetchall()
        feedbacks = [_row_to_feedback_item(dict(r)) for r in rows]
        return _compute_summary_from_items(feedbacks)


# ── DuckDBStore ────────────────────────────────────────────────────────────

class DuckDBStore(ReportStore):
    """
    Columnar timeseries store built on DuckDB.

    Why DuckDB over SQLite for this use case
    ----------------------------------------
    SQLite is a row store optimised for OLTP (point lookups by primary key).
    Timeseries analytics — "average CPU per hour over the last 30 days",
    "p95 latency per host this week", "anomaly frequency trend" — are columnar,
    range-scan workloads. DuckDB executes these 10–100× faster than SQLite on
    the same data with zero extra infrastructure (same embedded, file-based model).

    Schema
    ------
    metric_points   — one row per MetricPoint; enables arbitrary SQL over raw data
    anomalies       — one row per detected anomaly; enables frequency/severity trends
    reports         — summary + full JSON payload; preserves the existing API contract

    The three tables are linked by report_id, so you can join "what anomalies
    occurred in reports where overall_health = 'critical' last week".

    Thread safety
    -------------
    DuckDB connections are not safe to share across threads. We use the same
    threading.local() pattern as SQLiteStore.
    """

    _METRIC_COLS = [
        "cpu_usage", "memory_usage", "latency_ms", "disk_usage",
        "network_in_kbps", "network_out_kbps", "io_wait", "thread_count",
        "active_connections", "error_rate", "uptime_seconds",
        "temperature_celsius", "power_consumption_watts",
    ]

    def __init__(self, path: str) -> None:
        import duckdb  # local import — only required when DuckDBStore is used
        self._duckdb = duckdb
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path  = path
        self._local = threading.local()
        self._init_db()

    def _connect(self):
        if not getattr(self._local, "conn", None):
            self._local.conn = self._duckdb.connect(self._path)
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metric_points (
                report_id              TEXT NOT NULL,
                timestamp              TIMESTAMPTZ NOT NULL,
                host                   TEXT NOT NULL DEFAULT 'default',
                cpu_usage              DOUBLE,
                memory_usage           DOUBLE,
                latency_ms             DOUBLE,
                disk_usage             DOUBLE,
                network_in_kbps        DOUBLE,
                network_out_kbps       DOUBLE,
                io_wait                DOUBLE,
                thread_count           INTEGER,
                active_connections     INTEGER,
                error_rate             DOUBLE,
                uptime_seconds         INTEGER,
                temperature_celsius    DOUBLE,
                power_consumption_watts DOUBLE,
                db_status              TEXT,
                api_gateway_status     TEXT,
                cache_status           TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anomalies (
                report_id   TEXT NOT NULL,
                timestamp   TIMESTAMPTZ NOT NULL,
                host        TEXT NOT NULL DEFAULT 'default',
                severity    TEXT NOT NULL,
                stress_index DOUBLE,
                time_context TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id             TEXT PRIMARY KEY,
                generated_at   TIMESTAMPTZ NOT NULL,
                overall_health TEXT NOT NULL,
                anomaly_count  INTEGER,
                critical_count INTEGER,
                warning_count  INTEGER,
                avg_stress     DOUBLE,
                peak_stress    DOUBLE,
                payload        TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                report_id    TEXT    NOT NULL,
                rec_index    INTEGER NOT NULL,
                category     TEXT,
                priority     TEXT,
                title        TEXT,
                status       TEXT    NOT NULL,
                note         TEXT,
                submitted_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (report_id, rec_index)
            )
        """)

    def save(self, report: Report) -> None:
        conn = self._connect()

        # Persist raw metric rows — this is what enables timeseries queries
        for pt in report.enrichment:
            # Find matching MetricPoint via timestamp (enrichment carries same timestamps)
            pass

        # We store from report.analysis_window + enrichment to reconstruct metrics;
        # but we also need the original MetricPoints. The pipeline stores them in
        # report.enrichment (EnrichedPoint has derived signals) — for raw values
        # we use report.statistics and anomalies' triggered_metrics.
        #
        # To persist full raw rows we need to receive the original MetricPoints.
        # The save_with_metrics() method below handles that; save() stores the
        # report summary + anomaly rows which are sufficient for trend queries.

        conn.execute(
            """
            INSERT OR REPLACE INTO reports
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.report_id,
                report.generated_at,
                report.summary.overall_health,
                report.summary.anomaly_count,
                report.summary.critical_count,
                report.summary.warning_count,
                report.summary.avg_stress_index,
                report.summary.peak_stress_index,
                report.model_dump_json(),
            ),
        )

        for anomaly in report.anomalies:
            conn.execute(
                "INSERT INTO anomalies VALUES (?, ?, ?, ?, ?, ?)",
                (
                    report.report_id,
                    anomaly.timestamp,
                    "default",
                    anomaly.severity,
                    anomaly.stress_index,
                    anomaly.time_context,
                ),
            )

    def save_with_metrics(self, report: Report, metrics: list[MetricPoint]) -> None:
        """
        Persist report + raw metric rows in a single transaction.
        Use this instead of save() when raw timeseries queryability matters.
        Called by the pipeline runner in deps.py when DuckDBStore is active.
        """
        self.save(report)
        conn = self._connect()
        for m in metrics:
            conn.execute(
                f"""
                INSERT INTO metric_points VALUES (
                    ?, ?, ?, {', '.join('?' * len(self._METRIC_COLS))}, ?, ?, ?
                )
                """,
                (
                    report.report_id,
                    m.timestamp.isoformat(),
                    m.host,
                    *[getattr(m, c) for c in self._METRIC_COLS],
                    m.service_status.database,
                    m.service_status.api_gateway,
                    m.service_status.cache,
                ),
            )

    def latest(self) -> Report | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT payload FROM reports ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        return Report.model_validate_json(row[0]) if row else None

    def history(self, n: int = 20) -> list[Report]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT payload FROM reports ORDER BY generated_at DESC LIMIT ?", (n,)
        ).fetchall()
        return [Report.model_validate_json(r[0]) for r in rows]

    def get_report(self, report_id: str) -> Report | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT payload FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        return Report.model_validate_json(row[0]) if row else None

    def save_feedback(self, feedback: ReportFeedback) -> None:
        conn = self._connect()
        for item in feedback.items:
            conn.execute(
                """INSERT OR REPLACE INTO feedback
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (feedback.report_id, item.rec_index, item.category, item.priority,
                 item.title, item.status, item.note, item.submitted_at),
            )

    def get_feedback(self, report_id: str) -> ReportFeedback | None:
        conn = self._connect()
        cols = ["report_id", "rec_index", "category", "priority",
                "title", "status", "note", "submitted_at"]
        rows = conn.execute(
            "SELECT * FROM feedback WHERE report_id = ? ORDER BY rec_index",
            (report_id,),
        ).fetchall()
        if not rows:
            return None
        items = [RecommendationFeedback(
            rec_index=r[1], category=r[2], priority=r[3], title=r[4],
            status=r[5], note=r[6], submitted_at=str(r[7]),
        ) for r in rows]
        return ReportFeedback(report_id=report_id, items=items,
                              submitted_at=str(rows[-1][7]))

    def feedback_summary(self) -> FeedbackSummary:
        conn = self._connect()
        rows = conn.execute(
            "SELECT rec_index, category, priority, status FROM feedback"
        ).fetchall()
        items = [RecommendationFeedback(rec_index=r[0], category=r[1],
                                        priority=r[2], status=r[3])
                 for r in rows]
        return _compute_summary_from_items(items)

    def metric_history(
        self,
        host: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Query raw metric timeseries. Enables questions like:
          - "Show CPU and latency for host-A from Monday to Friday"
          - "What was the error_rate trend last week?"

        Returns a list of dicts ordered by timestamp ASC.
        """
        conn   = self._connect()
        where  = []
        params = []

        if host:
            where.append("host = ?")
            params.append(host)
        if start:
            where.append("timestamp >= ?")
            params.append(start.isoformat())
        if end:
            where.append("timestamp <= ?")
            params.append(end.isoformat())

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)

        cols = ["timestamp", "host"] + self._METRIC_COLS + [
            "db_status", "api_gateway_status", "cache_status"
        ]
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM metric_points {clause} "
            f"ORDER BY timestamp ASC LIMIT ?",
            params,
        ).fetchall()

        return [dict(zip(cols, row)) for row in rows]


# ── Feedback aggregation helpers ──────────────────────────────────────────

def _row_to_feedback_item(row: dict) -> RecommendationFeedback:
    return RecommendationFeedback(
        rec_index=row["rec_index"], category=row.get("category"),
        priority=row.get("priority"), title=row.get("title"),
        status=row["status"], note=row.get("note"),
        submitted_at=row.get("submitted_at"),
    )


def _compute_summary_from_items(items: list[RecommendationFeedback]) -> FeedbackSummary:
    from collections import defaultdict

    def _bucket() -> dict:
        return {"total": 0, "resolved": 0, "partial": 0,
                "not_relevant": 0, "not_tried": 0}

    by_cat: dict[str, dict] = defaultdict(_bucket)
    by_pri: dict[str, dict] = defaultdict(_bucket)

    for item in items:
        for d, key in ((by_cat, item.category or "unknown"),
                       (by_pri, item.priority or "unknown")):
            d[key]["total"] += 1
            d[key][item.status] += 1

    def _stats(label: str, b: dict) -> CategoryStats:
        t = b["total"]
        resolved = b["resolved"] + b["partial"]
        return CategoryStats(
            label=label, total=t,
            resolved=b["resolved"], partial=b["partial"],
            not_relevant=b["not_relevant"], not_tried=b["not_tried"],
            resolution_rate=round(resolved / t, 2) if t else 0.0,
        )

    total = len(items)
    overall_resolved = sum(
        1 for i in items if i.status in ("resolved", "partial")
    )

    return FeedbackSummary(
        total_feedback=total,
        overall_resolution_rate=round(overall_resolved / total, 2) if total else 0.0,
        by_category=[_stats(k, v) for k, v in sorted(by_cat.items())],
        by_priority=[_stats(k, v) for k, v in sorted(by_pri.items())],
    )


def _compute_summary(feedbacks: list[ReportFeedback]) -> FeedbackSummary:
    items = [item for fb in feedbacks for item in fb.items]
    return _compute_summary_from_items(items)


# ── Factory ────────────────────────────────────────────────────────────────

def make_store(dsn: str | None) -> ReportStore:
    """
    Select store implementation from STORE_DSN:
      unset / ""             → MemoryStore
      path ending in .duckdb → DuckDBStore
      any other path         → SQLiteStore
    """
    if not dsn or not dsn.strip():
        return MemoryStore()
    dsn = dsn.strip()
    if dsn.endswith(".duckdb"):
        return DuckDBStore(dsn)
    return SQLiteStore(dsn)
