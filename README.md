# InfraOptimizer

On-premise infrastructure anomaly detection and recommendation engine, powered by LLM.

Designed for CTOs who want AI-assisted infrastructure analysis without sending data to a cloud
service — the LLM call is the only external dependency, and it can be swapped for a local model.

---

## Architecture

```
HTTP POST /api/analyze
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│              FastAPI  app/api.py + app/routers/              │
│   auth · rate-limit · gzip · CORS · webhooks · SSE           │
└──────────────────────┬───────────────────────────────────────┘
                       │ deps.run_pipeline(request)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│           LangGraph Pipeline  app/graph.py                   │
│                                                              │
│  [ingest] → [enrich] → [analyze] → [recommend] → [report]   │
│                                                              │
│  • ingest    app/nodes/ingest.py    — extension point        │
│  • enrich    app/nodes/enrich.py    — trend · stress index   │
│                                       sustained · time ctx   │
│  • analyze   app/nodes/analyze.py   — threshold detection    │
│                                       escalation rules       │
│  • recommend app/nodes/recommend.py — LLM recommendations    │
│  • report    app/nodes/report.py    — assemble Report model  │
└──────────────────────┬───────────────────────────────────────┘
                       │ Report + raw MetricPoints
                       ▼
┌──────────────────────────────────────────────────────────────┐
│              ReportStore  app/store.py                       │
│  MemoryStore (default) │ SQLiteStore │ DuckDBStore           │
│                                        ↑                     │
│                              timeseries queries              │
│                              GET /api/metrics/history        │
└──────────────────────────────────────────────────────────────┘
```

### Why LangGraph?

The pipeline has a clear, linear shape today (5 nodes, no branching). LangGraph is deliberately
chosen as an **extension point**: conditional routing (e.g. skip LLM when no anomalies), parallel
sub-graphs (analyze hardware and services simultaneously), or retry loops are one edge away.
If the pipeline never grows, it can be replaced by five function calls with zero other changes.

### Why enrich before analyze?

Raw metrics alone are ambiguous. A CPU at 78% means different things depending on whether
it has been rising for two hours, is a one-off spike, or occurs at 3 AM on a Sunday.
The enrichment node resolves this ambiguity *before* threshold comparison, so:
- Sustained warning-level metrics during off-peak are escalated to critical
- A rising trend on an already-warning metric is escalated to critical
- The LLM receives trend direction and time context, not just raw values

This keeps escalation logic explicit and auditable (in `_analyze`, not hidden in prompts).

### Why rule-based anomaly detection instead of ML?

Statistical thresholds are transparent, deterministic, and require no training data. The LLM is
used only for *interpretation* (natural language recommendations), not for detection. This means
the detection logic is auditable and tweakable without data science expertise.

### Loose coupling

| Boundary              | How it's decoupled                                      |
|-----------------------|---------------------------------------------------------|
| LLM provider          | `LLM_MODEL` env var; client isolated in `app/nodes/recommend.py` |
| Storage backend       | `ReportStore` ABC — swap MemoryStore → SQLiteStore → DuckDBStore via `STORE_DSN` |
| External integrations | Webhook fan-out after analysis; Prometheus `/metrics` scrape target |
| API ↔ pipeline        | `graph.run()` is a pure function; API is just a transport layer  |

---

## Quickstart

```bash
# 1. Copy and fill the env file
cp .env.example .env

# 2a. Run locally
pip install -r requirements.txt
python main.py

# 2b. Or with Docker (recommended for on-premise)
docker compose up --build

# 3. Open the dashboard
open http://localhost:8000

# 4. Or analyze from CLI (no server needed)
python main.py --file data/metrics.json --out output/report.json
```

---

## Configuration

| Variable             | Default                  | Description                                        |
|----------------------|--------------------------|----------------------------------------------------|
| `ANTHROPIC_API_KEY`  | —                        | Required. Your Anthropic API key.                  |
| `LLM_MODEL`          | `claude-opus-4-6`        | Any Anthropic model ID.                            |
| `HOST`               | `0.0.0.0`                | Bind address.                                      |
| `PORT`               | `8000`                   | Listen port.                                       |
| `API_KEY`            | *(unset = no auth)*      | If set, all API routes require `X-API-Key` header. |
| `STORE_DSN`          | *(unset = memory)*       | `./data/metrics.duckdb` (DuckDB) or `./data/reports.db` (SQLite). |
| `WEBHOOK_URLS`       | *(empty)*                | Comma-separated URLs to POST the report to.        |
| `PROMETHEUS_ENABLED` | `true`                   | Expose `/metrics` in Prometheus text format.       |
| `MAX_METRICS`        | `5000`                   | Maximum data points accepted per request.          |
| `CORS_ORIGINS`       | `*`                      | Comma-separated allowed origins.                   |

---

## API Reference

| Method   | Path                      | Description                                               |
|----------|---------------------------|-----------------------------------------------------------|
| `POST`   | `/api/analyze`            | Run the pipeline. Body: `AnalyzeRequest` JSON.            |
| `GET`    | `/api/report/latest`      | Retrieve the last generated report.                       |
| `GET`    | `/api/reports`            | List report history (last n, descending).                 |
| `GET`    | `/api/metrics/history`    | Raw timeseries query — host, start, end filters (DuckDB). |
| `POST`   | `/api/metrics`            | Push points to the streaming buffer.                      |
| `GET`    | `/api/metrics/buffer`     | Buffer status (size / capacity).                          |
| `DELETE` | `/api/metrics/buffer`     | Clear the buffer.                                         |
| `POST`   | `/api/metrics/analyze`    | Drain buffer and run pipeline.                            |
| `GET`    | `/api/events`             | SSE stream — live report push.                            |
| `GET`    | `/metrics`                | Prometheus scrape endpoint.                               |
| `GET`    | `/health`                 | Liveness probe (`{"status":"ok"}`).                       |
| `GET`    | `/`                       | Dashboard (HTML).                                         |
| `GET`    | `/docs`                   | Interactive OpenAPI docs (Swagger UI).                    |

### Request body — POST /api/analyze

```json
{
  "metrics": [ { "timestamp": "...", "cpu_usage": 93, ... } ],
  "webhooks": ["https://hooks.slack.com/..."]   // optional, per-request
}
```

---

## Integrations

### Grafana
Two options — choose based on your setup:

**Option A — Prometheus scrape (recommended)**
Add a scrape job in `prometheus.yml`:
```yaml
- job_name: infra-optimizer
  static_configs:
    - targets: ['infra-optimizer:8000']
```
Then build dashboards from the `infra_*` metrics family.

**Option B — Webhook**
Set `WEBHOOK_URLS=http://grafana:3000/api/webhooks/...` in `.env`.
The full report JSON is POSTed after each analysis.

### Datadog / Centreon / Nagios
Poll `GET /api/report/latest` from your monitoring agent.
The response is stable JSON — map `summary.overall_health` to your status model.

### Slack / Teams
Add the incoming webhook URL to `WEBHOOK_URLS`. The report JSON is sent as the body.
Use a Slack workflow to parse and format the message.

---

## Security

| Concern          | Implementation                                                  |
|------------------|-----------------------------------------------------------------|
| Authentication   | `X-API-Key` header (set `API_KEY` env var to enable)           |
| Transport        | Terminate TLS at your reverse proxy (nginx/Traefik). Not in-app. |
| Input size       | `MAX_METRICS` cap + Pydantic validation on every field          |
| SSRF             | Webhook URLs are validated — private/loopback IPs are rejected  |
| Compression      | GZip middleware on all responses                                |
| CORS             | Configurable via `CORS_ORIGINS`                                 |

> **Note:** For production, place behind nginx/Traefik with TLS. The app handles auth
> and validation; TLS termination belongs at the edge.

---

## Performance / Load

| Setup                          | Approximate throughput             |
|--------------------------------|------------------------------------|
| Default (1 worker, memory)     | ~5–10 concurrent users before LLM queuing |
| `uvicorn --workers 4`          | 4× parallel pipelines              |
| Celery + Redis task queue      | Decoupled, async — handles bursts  |
| Local LLM (Ollama/ Gemma4)             | No external latency, fully air-gapped |

The LLM call (~3–5 s) is the bottleneck. Rule-based detection is O(n·m) — negligible for
typical metric windows (< 5 000 points).

For high-frequency ingestion (Prometheus scrape every 15 s), push raw metrics into
TimescaleDB directly and run the pipeline on a schedule, not per-request.

---

## Storage upgrade path

| Store        | When to use                                           | How to enable                        |
|--------------|-------------------------------------------------------|--------------------------------------|
| Memory       | Development, demos, stateless deployments             | Default (no config)                  |
| SQLite       | On-premise, simple persistence, no analytics          | `STORE_DSN=./data/reports.db`        |
| DuckDB       | On-premise + timeseries queries, multi-host analytics | `STORE_DSN=./data/metrics.duckdb`    |
| PostgreSQL   | Multi-instance, shared state                          | `STORE_DSN=postgresql://...`         |
| TimescaleDB  | Long-term metrics, Grafana native time-series panels  | Same DSN as PostgreSQL + extension   |

---

## Extending

### Add a new metric / threshold
Edit `thresholds.json` (or `THRESHOLDS` in `app/configuration.py`). One dict entry, no other change needed.

### Swap the LLM provider
Replace the `anthropic.Anthropic()` client and `client.messages.create(...)` call
in `app/nodes/recommend.py` — all other nodes are provider-agnostic.
For a fully local setup, point to an Ollama-compatible endpoint.

### Add a pipeline node
Add a file under `app/nodes/`, then register it with `add_node()` + `add_edge()` in
`app/graph.py`. The `State` TypedDict in `app/state.py` is the only shared contract.
