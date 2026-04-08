# InfraOptimizer — Product Roadmap

> **Current release:** V0 — functional on-premise prototype  
> **Goal:** production-grade infrastructure intelligence platform for SMEs

Each milestone is independent. Items within a milestone are ordered by dependency,
not priority — priority is set per-deployment based on the client's constraints.

---

## V0 — Prototype ✓ (current)

On-premise foundation. Demonstrates the full pipeline end-to-end.

| Area | Status |
|---|---|
| LangGraph pipeline: ingest → enrich → analyze → recommend → report | ✓ |
| Rule-based anomaly detection with severity escalation | ✓ |
| LLM recommendations (Anthropic Claude) | ✓ |
| REST API (FastAPI) + Swagger UI | ✓ |
| Streaming buffer + SSE live feed | ✓ |
| Simple HTML dashboard | ✓ |
| MemoryStore / SQLiteStore / DuckDBStore | ✓ |
| Prometheus `/metrics` scrape endpoint | ✓ |
| Docker + docker-compose | ✓ |
| API key auth (X-API-Key header) | ✓ |

---

## V1 — Production Hardening

*Prerequisite before any real-traffic deployment.*

### Security

- **OAuth2 / LDAP / SSO** — replace static API key with a proper identity provider.  
  Recommended stack: [Authlib](https://docs.authlib.org) for OAuth2 flows (Azure AD, Okta, Google Workspace);
  `python-ldap` for on-premise Active Directory.  
  Adds: JWT validation dependency, `/auth/login` and `/auth/callback` routes.

- **Role-based access control (RBAC)** — at minimum: `viewer` (read reports) vs `operator`
  (trigger analysis, clear buffer) vs `admin` (manage config). Enforced as FastAPI dependencies.

- **TLS in-app** — V0 delegates TLS to a reverse proxy. For deployments without nginx/Traefik,
  add `ssl_keyfile` / `ssl_certfile` to uvicorn startup and auto-renew via Let's Encrypt or
  internal PKI.

- **Audit log** — append-only log of every analysis run, who triggered it, and the outcome.
  Critical for regulated industries (ISO 27001, SOC 2). One extra table in the store.

### Reliability

- **Async LLM client** — the current `anthropic.Anthropic()` is synchronous, wrapped in
  `run_in_executor`. Migrate to `anthropic.AsyncAnthropic()` so the thread pool is not
  exhausted under concurrent load.

- **Pipeline retry / dead-letter queue** — wrap the `recommend` node in a retry loop
  (exponential backoff) for transient LLM errors. Failed jobs go to a dead-letter store
  so operators can replay them.

- **Health check depth** — current `/health` is a liveness probe only. Add a readiness probe
  (`/health/ready`) that checks store connectivity and LLM reachability before accepting traffic.

- **Structured error responses** — standardise all 4xx/5xx responses to
  `{ "error": "...", "code": "PIPELINE_TIMEOUT", "request_id": "..." }` for easier client
  error handling.
  
- **AI Steward** Add an Ai steward to double validate the first LLM analysis call.

### Operations

- **Helm chart / Kubernetes manifests** — for teams running k8s. Deployment + Service +
  ConfigMap for env vars + PersistentVolumeClaim for the DuckDB file.

- **CI pipeline** — GitHub Actions / GitLab CI running `pytest`, `ruff`, `mypy` on every push.
  Docker image built and pushed to registry on tag.

---

## V2 — Data & Analytics

*Unlocks long-term value extraction from collected metrics.*

### Storage

- **TimescaleDB** — production-grade time-series on top of PostgreSQL.  
  Replaces DuckDB for multi-node / high-ingestion deployments.  
  Add a `TimescaleStore(ReportStore)` implementation; the `ReportStore` ABC means
  no other code changes. Enables: continuous aggregates, automatic data downsampling,
  native Grafana datasource.

- **InfluxDB** — alternative for teams already running the TICK stack.  
  Trade-off vs TimescaleDB: better write throughput, weaker SQL, no joins.

- **Data retention policies** — auto-expire raw metric rows after N days (configurable).
  Keep report summaries and anomaly rows forever. Prevents unbounded disk growth.

- **Multi-host analytics** — the `host` field is already on `MetricPoint`.
  Expose aggregated queries: `GET /api/metrics/hosts` (list), cross-host anomaly
  correlation (did a spike on `web-01` precede a spike on `db-01`?).

### Querying

- **`GET /api/metrics/aggregate`** — time-bucketed aggregates: average/p95 per metric,
  per host, per hour/day. Powers Grafana panels without exporting raw rows.

- **`GET /api/anomalies/trends`** — anomaly frequency over time. Answers "are incidents
  becoming more frequent?" — the CTO's primary question for capacity planning.

- **Export endpoint** — `GET /api/export?format=csv&start=...&end=...` for feeding
  BI tools (Power BI, Tableau, Metabase).

---

## V3 — User Experience

*Replaces the prototype HTML dashboard with a maintainable frontend.*

### React Frontend

Replace `dashboard/index.html` (single-file, vanilla JS) with a proper React application.

**Recommended stack:**
- [Vite](https://vitejs.dev) + React + TypeScript
- [Recharts](https://recharts.org) or [Tremor](https://tremor.so) for data visualisation
- [TanStack Query](https://tanstack.com/query) for API data fetching and caching
- [shadcn/ui](https://ui.shadcn.com) for components

**Key views:**
- **Overview** — current health status, stress index gauge, active anomaly count
- **Timeline** — multi-metric chart over selectable time range, anomaly markers overlaid
- **Anomaly log** — filterable table (severity, host, metric, date range)
- **Recommendations** — cards with priority badges, expandable action lists
- **Multi-host** — side-by-side comparison, heatmap view
- **Config** — threshold editor UI (reads/writes `thresholds.json` via new API endpoint)

**Delivery:** static build served by FastAPI `StaticFiles`, or a separate nginx container.
No separate frontend server required for on-premise deployments.

### Mobile / Responsive

Current dashboard is desktop-only. React rebuild targets responsive layout for
on-call engineers checking status from a phone.

---

## V4 — Intelligence

*Moves detection from rule-based to adaptive.*

### Anomaly Detection

- **Baseline learning** — instead of static thresholds, compute rolling baselines
  (mean ± 2σ over the last 7 days) per host per metric. Eliminates false positives
  caused by legitimate workload growth. New pipeline node: `baseline`.

- **Seasonality awareness** — detect that "CPU at 80% on Monday morning" is normal
  for this system. Uses [statsmodels](https://www.statsmodels.org) STL decomposition
  or a lightweight Prophet model.

- **Multi-metric correlation** — detect cascading failure patterns: high CPU +
  rising latency + degraded cache often precede a full outage. LangGraph conditional
  edge from `analyze` to a `correlate` node.

- **Predictive alerts** — given current trends, forecast when a metric will breach
  its threshold. "Disk will reach critical in ~4 hours at current write rate."
  Gives the operator time to act before the incident, not after.

### LLM

- **Local LLM support** — swap Anthropic for [Ollama](https://ollama.com) running
  `llama3`, `mistral`, or `qwen`. Full air-gap compliance; no data leaves the premises.
  One env var change: `LLM_BASE_URL=http://ollama:11434`.

- **Feedback loop** — operators mark recommendations as "acted on" or "not relevant".
  Feed this back into the LLM prompt as few-shot examples to improve recommendation
  quality over time for this specific infrastructure.

- **Multi-language recommendations** — currently French (system prompt). Add `REPORT_LANG`
  env var to support EN, FR, DE, ES without prompt duplication.

---

## V5 — Enterprise

*For clients with multiple teams, compliance requirements, or multi-site deployments.*

### Multi-tenancy

- Separate data namespaces per team/department — one deployment, isolated data.
- Configurable thresholds per tenant (the CTO's team may have different SLAs than DevOps).
- Per-tenant webhook targets and notification preferences.

### Compliance & Governance

- **GDPR data map** — document which fields constitute personal data (IP addresses in
  `host` names, usernames in service names). Implement configurable field masking.
- **Immutable audit trail** — all analysis runs, config changes, and data exports logged
  to an append-only table. Required for ISO 27001 / SOC 2 / HDS (French healthcare).
- **Data residency** — configuration option to enforce that all data (including LLM calls)
  stays within a specified geographic boundary. Requires local LLM (V4).

### Integrations

| System | Integration |
|---|---|
| **PagerDuty / OpsGenie** | POST alert on `critical` severity anomaly |
| **Slack / Teams** | Rich message card with anomaly summary and one-click ack |
| **Jira / Linear** | Auto-create incident ticket from critical report |
| **Grafana** | Native datasource plugin (queries `/api/metrics/aggregate`) |
| **Datadog / Dynatrace** | Export metrics via StatsD or OTLP (OpenTelemetry) |
| **SIEM (Splunk, ELK)** | Forward structured logs via syslog or Kafka topic |

---

## Not in scope (by design)

| Item | Reason |
|---|---|
| Agent/metric collection | InfraOptimizer is an *analysis* layer, not a collector. Prometheus, Telegraf, or Datadog already handle collection. |
| Cloud-hosted SaaS version | The explicit requirement is on-premise. A SaaS offering is a separate product decision. |
| Real-time ML training | Batch retraining nightly is sufficient for infrastructure anomaly patterns at SME scale. |
| Mobile app | The React frontend (V3) covers the responsive use case. A native app adds maintenance cost for marginal UX gain. |

---

## Dependency map

```
V0 (done)
  └─ V1 (hardening)       ← prerequisite for any production use
       ├─ V2 (data)        ← prerequisite for V4 intelligence features
       │    └─ V4 (intelligence)
       ├─ V3 (frontend)    ← independent of V2/V4, can ship in parallel
       └─ V5 (enterprise)  ← requires V1 security + V2 multi-host data
```
