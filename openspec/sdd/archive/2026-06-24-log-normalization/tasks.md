# Log Normalization — Implementation Tasks

**Change ID**: `log-normalization`
**Status**: Tasks
**Date**: 2026-06-24
**Owner**: manudiv16
**Depends on**: proposal.md, spec.md, design.md

---

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 650–850 |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 → Tansu infra · PR 2 → OTel Collector logs pipeline · PR 3 → Query layer (Trino + Arroyo) |
| Delivery strategy | ask-on-risk |
| Chain strategy | stacked-to-main |

```text
Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High
```

### PR Split Rationale

Each PR is independently deployable and verifiable. ArgoCD sync waves handle runtime ordering.

| PR | Scope | Est. Lines | Deploys | Verifies |
|----|-------|------------|---------|----------|
| PR 1 | Tansu infrastructure | ~250 | PostgreSQL + Tansu StatefulSet/Service/Secret + ArgoCD app | Tansu pods running, ports 4317/9092 listening |
| PR 2 | OTel Collector logs pipeline | ~300 | Updated collector.yaml | Collector pods not OOMKilling, logs arriving at Tansu |
| PR 3 | Query layer | ~250 | Trino + Arroyo + ArgoCD apps | Trino queries Q1-Q5 return results |

---

## Phase 1: Tansu Infrastructure (DB + Broker)

PR 1 scope. All files are new. No dependency on Phase 2 or 3.

### T1.1 — Create Tansu PostgreSQL StatefulSet and Service

**Effort**: S | **Type**: Git-managed

Create a dedicated PostgreSQL instance for Tansu's internal state storage.

**Files**:
- `infrastructure/tansu/postgres-statefulset.yaml` — PostgreSQL 16-alpine StatefulSet, 2Gi PVC, readiness probe via `pg_isready`
- `infrastructure/tansu/postgres-service.yaml` — ClusterIP Service on port 5432

**Key details**:
- Image: `postgres:16-alpine`
- Namespace: `monitoring`
- Resources: 50m/128Mi request, 250m/256Mi limit
- Environment: `POSTGRES_DB=tansu`, `POSTGRES_USER=tansu`, password from `tansu-secrets` Secret
- PVC: 2Gi, `ReadWriteOnce`

**Dependencies**: None (first task)

**Verification**: `kubectl -n monitoring get pods -l app.kubernetes.io/name=tansu-pg` → Running; `pg_isready` probe passes

---

### T1.2 — Create Tansu Secret

**Effort**: S | **Type**: Git-managed (template) + Manual (populate real credentials)

Create the Secret containing PostgreSQL DSN and Scaleway S3 credentials.

**Files**:
- `infrastructure/tansu/secret.yaml` — Opaque Secret with `postgres-dsn`, `s3-access-key`, `s3-secret-key`

**Key details**:
- `postgres-dsn`: `postgresql://tansu:<password>@tansu-pg.monitoring.svc.cluster.local:5432/tansu?sslmode=disable`
- S3 credentials: placeholders referencing Scaleway S3 keys (must be replaced before deployment or managed via sealed-secrets/external-secrets)
- Commit a template with placeholder values; document that real values must be set via `kubectl create secret` or external-secrets operator

**Dependencies**: None

**Verification**: `kubectl -n monitoring get secret tansu-secrets` exists (after manual credential injection)

**Decision point**: If the homelab already uses sealed-secrets or external-secrets, integrate with that system instead of committing a plaintext template. Ask the user if unsure.

---

### T1.3 — Create Tansu Broker StatefulSet

**Effort**: M | **Type**: Git-managed

Deploy the Tansu Kafka-compatible broker with OTLP ingestion and native Iceberg writes.

**Files**:
- `infrastructure/tansu/statefulset.yaml` — Tansu StatefulSet

**Key details**:
- Image: `ghcr.io/rustyconover/tansu:latest` (pin tag after first successful deploy)
- Args: `--all-features`
- Environment variables for PostgreSQL backend, Kafka listener (PLAINTEXT://0.0.0.0:9092), OTLP listener (0.0.0.0:4317), Iceberg REST catalog config, S3 warehouse path
- S3 endpoint: `https://s3.fr-par.scw.cloud` (Scaleway)
- Iceberg warehouse: `s3://logs/iceberg`
- Iceberg namespace: `logs`
- 5Gi PVC for Tansu data (`volumeClaimTemplates`)
- Resources: 100m/256Mi request, 500m/512Mi limit
- Readiness probe: TCP socket port 9092
- Liveness probe: TCP socket port 9092
- ServiceAccount: `tansu-broker`

**Dependencies**: T1.1 (PostgreSQL running), T1.2 (Secret exists)

**Verification**: Pod running, ports 4317 and 9092 accepting TCP connections

---

### T1.4 — Create Tansu Service

**Effort**: S | **Type**: Git-managed

Expose Tansu as a ClusterIP Service with OTLP gRPC and Kafka ports.

**Files**:
- `infrastructure/tansu/service.yaml` — ClusterIP Service

**Key details**:
- Name: `tansu-broker` (matches OTel Collector exporter endpoint `tansu-broker.monitoring.svc.cluster.local:4317`)
- Ports: 4317 (otlp-grpc), 9092 (kafka)
- Selector: `app.kubernetes.io/name: tansu`

**Dependencies**: T1.3 (StatefulSet defines the pod labels)

**Verification**: `kubectl -n monitoring get svc tansu-broker` shows both ports; `kubectl -n monitoring exec -it deploy/collector-daemonset -- nc -zv tansu-broker.monitoring.svc.cluster.local 4317` (after collector is deployed)

---

### T1.5 — Create Tansu RBAC (if needed)

**Effort**: S | **Type**: Git-managed

Create a ServiceAccount for Tansu. Add RBAC rules only if Tansu needs Kubernetes API access (e.g., for CRD-based catalog). Skip RBAC resources if Tansu only needs S3 + PostgreSQL access.

**Files**:
- `infrastructure/tansu/rbac.yaml` — ServiceAccount + optional Role/RoleBinding

**Key details**:
- Minimum: ServiceAccount `tansu-broker` in namespace `monitoring`
- Add ClusterRole/Binding only if Tansu's Iceberg catalog needs K8s API access

**Dependencies**: None

**Verification**: `kubectl -n monitoring get sa tansu-broker`

---

### T1.6 — Create ArgoCD Application for Tansu

**Effort**: S | **Type**: Git-managed

Define the ArgoCD Application that deploys all Tansu manifests.

**Files**:
- `apps/tansu.yaml` — ArgoCD Application

**Key details**:
- Source path: `infrastructure/tansu`
- Sync wave: `3` (matches existing FluentBit, otel-gateway wave)
- Automated sync: prune + selfHeal
- CreateNamespace: true
- Namespace: `monitoring`
- `directory.recurse: true`

**Dependencies**: T1.1–T1.5 (all Tansu manifests must exist)

**Verification**: `argocd app get tansu` → Synced + Healthy; pods running in `monitoring` namespace

---

## Phase 2: OTel Collector Logs Pipeline

PR 2 scope. Single file modification (`collector.yaml`). Depends on Phase 1 being merged so Tansu Service exists when ArgoCD syncs.

### T2.1 — Add Volumes, VolumeMounts, and file_storage Extension

**Effort**: S | **Type**: Git-managed

Extend `collector.yaml` with hostPath volumes for container log access and filelog offset persistence.

**File**: `infrastructure/monitoring/otel-collector/collector.yaml`

**Changes**:
- Add `volumes` section (currently empty):
  - `varlog` → hostPath `/var/log`
  - `varlibdockercontainers` → hostPath `/var/lib/docker/containers`
  - `otel-registry` → hostPath `/var/lib/otel/registry` (DirectoryOrCreate)
- Add `volumeMounts` section:
  - `varlog` → `/var/log` (readOnly)
  - `varlibdockercontainers` → `/var/lib/docker/containers` (readOnly)
  - `otel-registry` → `/var/lib/otel`
- Add `file_storage` extension under `config.extensions`:
  - `directory: /var/lib/otel`
  - `timeout: 1s`
  - `compaction: { on_start: true, on_rebound: true, max_transaction_size: 65536 }`
- Add `extensions` list under `service.extensions`: `file_storage`

**Dependencies**: None (but merged as part of PR 2 with T2.2–T2.6)

**Verification**: `kubectl -n monitoring get ds collector-daemonset -o yaml` shows volumes and volumeMounts

---

### T2.2 — Add filelog Receiver with Container Parser and Conditional Operators

**Effort**: M | **Type**: Git-managed

Add the `filelog` receiver with CRI parsing, multiline support, and conditional format parsers.

**File**: `infrastructure/monitoring/otel-collector/collector.yaml`

**Changes** — add under `config.receivers`:
```yaml
filelog:
  include: [/var/log/containers/*.log]
  exclude: [/var/log/containers/fluentbit-*.log]
  storage: file_storage
  multiline:
    line_start_pattern: '^(\d{4}-\d{2}-\d{2}|[IWEF]\d{4}|Traceback|panic|goroutine \d+)'
    flush_after: 5s
  operators:
    - type: container          # CRI parser (containerd format)
    - type: json_parser        # Conditional: body starts with "{"
    - type: regex_parser       # Conditional: glog pattern
    - type: key_value_parser   # Conditional: ≥3 key=value pairs
```

**Key decisions**:
- `line_start_pattern` covers Java (ISO date), glog (IWEF prefix), Python (Traceback), Go (panic/goroutine)
- Exclude fluentbit pods to avoid feedback loops
- `storage: file_storage` references the extension from T2.1
- Each parser uses `if:` predicate and `on_error: send_quiet`

**Dependencies**: T2.1 (file_storage extension)

**Verification**: Collector pod logs show filelog receiver started; no parse errors in first 5 minutes

---

### T2.3 — Add transform/normalize-logs Processor

**Effort**: L | **Type**: Git-managed

Add the `transform` processor with ~60 OTTL statements for format detection, severity normalization, timestamp extraction, body cleanup, and attribute cleanup.

**File**: `infrastructure/monitoring/otel-collector/collector.yaml`

**Changes** — add under `config.processors`:
```yaml
transform/normalize-logs:
  log_statements:
    # Group 1: Format detection (4 statements)
    #   - Default: text
    #   - JSON: body startsWith "{"
    #   - glog: severity_letter present
    #   - KV: attributes["ts"] present
    # Group 2: Severity normalization (~30 statements)
    #   - Default: INFO/9
    #   - Check "level" field: trace/debug/info/warn/error/fatal
    #   - Check "severity" field: same mapping
    #   - glog fallback
    # Group 3: Timestamp normalization (~8 statements)
    #   - JSON: timestamp/ts/@timestamp fields
    #   - KV: ts/time fields
    #   - glog: handled by regex_parser
    #   - text: keep CRI timestamp
    # Group 4: Body cleanup (~5 statements)
    #   - JSON: move msg/message to body
    #   - glog: move message to body
    #   - KV: move msg/message to body
    # Group 5: Attribute cleanup (~5 statements)
    #   - Delete parser artifacts: severity_letter, source_file, source_line, thread_id, log.iostream
```

**Dependencies**: T2.2 (filelog receiver produces the attributes that transform consumes)

**Verification**: Deploy with a test JSON log; query Tansu/Iceberg for `attributes["log.format"] == "json"` and correct severity

---

### T2.4 — Add batch/logs Processor and otlp/tansu Exporter

**Effort**: S | **Type**: Git-managed

Add a dedicated batch processor and OTLP exporter for the logs pipeline.

**File**: `infrastructure/monitoring/otel-collector/collector.yaml`

**Changes**:
- Add `batch/logs` processor:
  - `timeout: 5s`
  - `send_batch_size: 512`
  - `send_batch_max_size: 1024`
- Add `otlp/tansu` exporter:
  - `endpoint: tansu-broker.monitoring.svc.cluster.local:4317`
  - `tls.insecure: true`
  - `retry_on_failure: { enabled: true, initial_interval: 5s, max_interval: 30s, max_elapsed_time: 300s }`
  - `sending_queue: { enabled: true, queue_size: 5000, num_consumers: 4 }`

**Dependencies**: T1.4 (Tansu Service must exist for the endpoint to resolve at runtime; retry handles startup race)

**Verification**: Collector config validates (`kubectl -n monitoring exec` collector pod — check config); exporter endpoint resolves via DNS

---

### T2.5 — Add Logs Pipeline to service.pipelines

**Effort**: S | **Type**: Git-managed

Wire the logs pipeline in the `service.pipelines` section.

**File**: `infrastructure/monitoring/otel-collector/collector.yaml`

**Changes** — add under `config.service.pipelines`:
```yaml
logs:
  receivers: [filelog]
  processors: [memory_limiter, k8sattributes, transform/normalize-logs, resource/cluster, batch/logs]
  exporters: [otlp/tansu]
```

**Dependencies**: T2.2 (filelog receiver), T2.3 (transform processor), T2.4 (batch/logs + exporter)

**Verification**: Collector pod shows both `metrics` and `logs` pipelines active in startup logs

---

### T2.6 — Increase Resource Limits and memory_limiter

**Effort**: S | **Type**: Git-managed

Increase the collector's resource budget to accommodate the logs pipeline.

**File**: `infrastructure/monitoring/otel-collector/collector.yaml`

**Changes**:
- `resources.requests.cpu`: 100m → 150m
- `resources.requests.memory`: 128Mi → 200Mi
- `resources.limits.cpu`: (none) → 500m
- `resources.limits.memory`: 512Mi → 600Mi
- `memory_limiter.limit_mib`: 400 → 500
- `memory_limiter.spike_limit_mib`: 100 → 150

**Dependencies**: None (but applied together with the rest of PR 2)

**Verification**: `kubectl -n monitoring describe ds collector-daemonset` shows updated resource requests/limits; no OOMKills in first 30 minutes

---

## Phase 3: Iceberg + Query Layer

PR 3 scope. All files are new. Depends on Phase 1 (Tansu running) but is independent of Phase 2 changes.

### T3.1 — Create Trino Deployment

**Effort**: M | **Type**: Git-managed

Deploy Trino as a single-replica coordinator for querying Iceberg tables.

**Files**:
- `infrastructure/iceberg-query/trino-deployment.yaml`

**Key details**:
- Image: `trinodb/trino:468`
- Namespace: `monitoring`
- Port: 8080 (HTTP)
- Resources: 250m/512Mi request, 1000m/1Gi limit
- Volume mount: catalog config from ConfigMap `trino-catalog-config` → `/etc/trino/catalog`
- Readiness probe: `GET /v1/info` on port 8080
- Liveness probe: `GET /v1/info` on port 8080

**Dependencies**: None (ConfigMap from T3.3 must exist before pod starts)

**Verification**: Pod running; `curl http://trino.monitoring.svc.cluster.local:8080/v1/info` → `{"state":""}`

---

### T3.2 — Create Trino Service

**Effort**: S | **Type**: Git-managed

Expose Trino as a ClusterIP Service.

**Files**:
- `infrastructure/iceberg-query/trino-service.yaml`

**Key details**:
- Name: `trino`
- Port: 8080
- Selector: `app.kubernetes.io/name: trino`

**Dependencies**: T3.1

**Verification**: `kubectl -n monitoring get svc trino` → port 8080

---

### T3.3 — Create Trino Iceberg Catalog ConfigMap

**Effort**: S | **Type**: Git-managed

Configure Trino's Iceberg connector to read from the Scaleway S3 warehouse.

**Files**:
- `infrastructure/iceberg-query/trino-catalog-configmap.yaml`

**Key details**:
- Catalog name: `iceberg` (file: `iceberg.properties`)
- `connector.name=iceberg`
- `iceberg.catalog.type=rest` (pointing to Tansu's REST catalog) or `hadoop` (direct S3)
- S3 endpoint: `https://s3.fr-par.scw.cloud`
- S3 region: `fr-par`
- S3 credentials: from environment variables or mounted Secret
- `iceberg.table-statistics-enabled=false` (avoids ANALYZE requirement)

**Decision point**: REST catalog vs Hadoop catalog. Design prefers REST, but if Tansu doesn't expose a REST catalog endpoint, fall back to Hadoop catalog with direct S3 path. Start with Hadoop catalog for simplicity.

**Dependencies**: T1.4 (Tansu Service, if using REST catalog)

**Verification**: Trino pod starts without catalog errors; `SHOW CATALOGS` includes `iceberg`

---

### T3.4 — Create ArgoCD Application for Iceberg Query

**Effort**: S | **Type**: Git-managed

Define the ArgoCD Application for the Trino deployment.

**Files**:
- `apps/iceberg-query.yaml`

**Key details**:
- Source path: `infrastructure/iceberg-query`
- Sync wave: `4`
- Automated sync: prune + selfHeal
- Namespace: `monitoring`

**Dependencies**: T3.1–T3.3

**Verification**: `argocd app get iceberg-query` → Synced + Healthy

---

### T3.5 — Create Arroyo Deployment

**Effort**: M | **Type**: Git-managed

Deploy Arroyo for streaming SQL second-pass normalization.

**Files**:
- `infrastructure/arroyo/deployment.yaml`

**Key details**:
- Image: `ghcr.io/arroyosystems/arroyo:latest` (pin tag after first deploy)
- Namespace: `monitoring`
- Port: 8000 (API)
- Resources: 100m/256Mi request, 500m/512Mi limit
- Environment:
  - `ARROYO__KAFKA_BOOTSTRAP_SERVERS`: `tansu-broker.monitoring.svc.cluster.local:9092`
  - `ARROYO__ICEBERG_CATALOG_URI`: Tansu REST catalog URI
  - `ARROYO__ICEBERG_WAREHOUSE`: `s3://logs/iceberg`
- Readiness probe: `GET /api/v1/status` on port 8000

**Dependencies**: T1.3 (Tansu broker running with Kafka endpoint)

**Verification**: Pod running; Arroyo API accessible on port 8000

---

### T3.6 — Create Arroyo Service

**Effort**: S | **Type**: Git-managed

Expose Arroyo as a ClusterIP Service.

**Files**:
- `infrastructure/arroyo/service.yaml`

**Key details**:
- Name: `arroyo`
- Port: 8000
- Selector: `app.kubernetes.io/name: arroyo`

**Dependencies**: T3.5

**Verification**: `kubectl -n monitoring get svc arroyo` → port 8000

---

### T3.7 — Create Arroyo Streaming SQL ConfigMap

**Effort**: M | **Type**: Git-managed

Define the Arroyo streaming SQL pipeline for second-pass normalization (multiline reassembly).

**Files**:
- `infrastructure/arroyo/configmap.yaml`

**Key details**:
- Contains SQL for:
  - `CREATE TABLE otel_logs_source` (Kafka source from Tansu `otel-logs` topic)
  - `CREATE TABLE normalized_logs` (tumbling window reassembly, 10s window)
  - `CREATE TABLE normalized_logs_sink` (Iceberg output to `otel_logs_normalized`)
- The SQL handles multiline stack trace reassembly within a 10-second window

**Dependencies**: T3.5

**Verification**: ConfigMap exists; Arroyo UI shows the pipeline definition

---

### T3.8 — Create ArgoCD Application for Arroyo

**Effort**: S | **Type**: Git-managed

Define the ArgoCD Application for the Arroyo deployment.

**Files**:
- `apps/arroyo.yaml`

**Key details**:
- Source path: `infrastructure/arroyo`
- Sync wave: `4`
- Automated sync: prune + selfHeal
- Namespace: `monitoring`

**Dependencies**: T3.5–T3.7

**Verification**: `argocd app get arroyo` → Synced + Healthy

---

## Phase 4: Verification & Tuning

Manual / cluster-operation tasks. Not part of any PR.

### T4.1 — Validate ArgoCD Sync Status

**Effort**: S | **Type**: Manual

Verify all new ArgoCD Applications are Synced and Healthy.

**Commands**:
```bash
argocd app get tansu
argocd app get otel-collector
argocd app get iceberg-query
argocd app get arroyo
```

**Expected**: All four apps show `Synced` + `Healthy`

**Dependencies**: PRs 1–3 merged

---

### T4.2 — Verify Collector Resource Stability

**Effort**: M | **Type**: Manual

Monitor collector DaemonSet pods for 30 minutes to confirm no OOMKills under production log volume.

**Commands**:
```bash
# Check for OOMKill events
kubectl -n monitoring get events --field-selector reason=OOMKilling

# Monitor memory usage
kubectl -n monitoring top pods -l app.kubernetes.io/name=collector-daemonset

# Check pod restarts
kubectl -n monitoring get pods -l app.kubernetes.io/name=collector-daemonset -o wide
```

**Expected**: Zero OOMKills; memory < 600Mi; zero restarts

**Dependencies**: PR 2 merged and synced

---

### T4.3 — Verify Logs Arriving at Tansu

**Effort**: S | **Type**: Manual

Confirm Tansu is receiving OTLP log records from the collector.

**Commands**:
```bash
# Check Tansu pod logs for OTLP ingestion
kubectl -n monitoring logs -l app.kubernetes.io/name=tansu --tail=100

# Check collector exporter metrics
kubectl -n monitoring exec -it ds/collector-daemonset -- \
  curl -s http://localhost:8888/metrics | grep otelcol_exporter_sent_log_records
```

**Expected**: Tansu logs show received records; `otelcol_exporter_sent_log_records` > 0

**Dependencies**: T4.2

---

### T4.4 — Validate Iceberg Table Exists and Is Queryable

**Effort**: M | **Type**: Manual

Verify the `otel_logs` Iceberg table was created by Tansu and is accessible via Trino.

**Commands**:
```bash
# Port-forward Trino
kubectl -n monitoring port-forward svc/trino 8080:8080

# Query table metadata
curl -s http://localhost:8080/v1/statement \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Catalog: iceberg" \
  -H "X-Trino-Schema: logs" \
  -d "SHOW TABLES"

# Query row count
curl -s http://localhost:8080/v1/statement \
  -H "X-Trino-User: admin" \
  -d "SELECT count(*) FROM iceberg.logs.otel_logs"
```

**Expected**: Table `otel_logs` exists; row count > 0

**Dependencies**: T4.3 (logs flowing into Tansu/Iceberg)

---

### T4.5 — Run Mandatory Queries Q1–Q5 via Trino

**Effort**: M | **Type**: Manual

Execute the 5 mandatory queries defined in the spec and verify they return results.

| Query | Description | Expected |
|-------|-------------|----------|
| Q1 | ERROR logs in last hour for a namespace | Non-empty result set |
| Q2 | Stack traces matching pattern | Non-empty if errors exist |
| Q3 | Log volume per namespace per hour | Per-namespace counts |
| Q4 | Error rate by service | Percentage per service |
| Q5 | Top log-producing pods | Top 20 pods with counts |

**Dependencies**: T4.4

---

### T4.6 — Validate DuckDB Ad-Hoc Queries

**Effort**: S | **Type**: Manual

Run the same queries via DuckDB CLI (local or pod exec) and verify results match Trino.

**Commands**:
```bash
duckdb -c "
  INSTALL iceberg; LOAD iceberg;
  INSTALL httpfs; LOAD httpfs;
  SET s3_endpoint = 's3.fr-par.scw.cloud';
  SET s3_region = 'fr-par';
  SET s3_access_key_id = getenv('SCALEWAY_S3_ACCESS_KEY');
  SET s3_secret_access_key = getenv('SCALEWAY_S3_SECRET_KEY');
  SET s3_url_style = 'path';
  SELECT count(*) FROM iceberg_scan('s3://logs/iceberg/logs/otel_logs/metadata');
"
```

**Expected**: Row count matches Trino result (±0 for same snapshot)

**Dependencies**: T4.4

---

### T4.7 — 24-Hour Parallel Validation

**Effort**: L | **Type**: Manual

Compare log counts between Iceberg (OTel pipeline) and Parquet (FluentBit pipeline) over a 24-hour window.

**Approach**:
1. Record Iceberg log count at T=0 and T=24h
2. Record FluentBit Parquet log count for the same window
3. Compare: delta must be within ±5%

**Commands** (DuckDB):
```sql
-- Iceberg count
SELECT count(*) FROM iceberg_scan('s3://logs/iceberg/logs/otel_logs/metadata')
WHERE timestamp > now() - INTERVAL '24' HOUR;

-- Parquet count (FluentBit output)
SELECT count(*) FROM read_parquet('s3://logs/raw/containers/year=2026/month=06/day=24/**/*.parquet');
```

**Expected**: Counts within ±5% (AC-15)

**Dependencies**: T4.3 (logs flowing for ≥ 24 hours)

---

### T4.8 — Adjust OTTL Rules Based on Real Log Patterns

**Effort**: M–L | **Type**: Git-managed (iterative)

Review real log patterns from the cluster and tune the `transform/normalize-logs` processor.

**Activities**:
1. Query `SELECT log_format, count(*) FROM otel_logs GROUP BY log_format` — identify format distribution
2. Query `SELECT * FROM otel_logs WHERE log_format = 'text' LIMIT 100` — find misclassified formats
3. For each misclassified pattern: add or adjust OTTL `where` clauses
4. For multiline false merges: tighten `line_start_pattern`
5. For multiline false splits: broaden `line_start_pattern` or add Arroyo reassembly rules
6. Commit changes, verify via ArgoCD sync

**Dependencies**: T4.5 (queries working), sufficient log volume

**Note**: This task may span multiple iterations. Each iteration is a small PR on top of PR 2.

---

## Dependency Graph

```
T1.1 (PG StatefulSet) ──┐
T1.2 (Secret) ──────────┤
T1.5 (RBAC) ────────────┼──→ T1.3 (Tansu StatefulSet) ──→ T1.4 (Tansu Service) ──┐
                         │                                                          │
                         └──────────────────────────────────────────────────────────→ T1.6 (ArgoCD App)
                                                                                     │
                                                                                     ▼
                                                                               PR 1 MERGED
                                                                                     │
T2.1 (Volumes + ext) ──→ T2.2 (filelog) ──→ T2.3 (transform) ─┐                    │
T2.6 (Resources) ──────────────────────────────────────────────┼──→ T2.5 (pipeline) │
T2.4 (batch + exporter) ──────────────────────────────────────┘         │            │
                                                                        ▼            │
                                                                  PR 2 MERGED        │
                                                                        │            │
T3.3 (Trino ConfigMap) ──→ T3.1 (Trino Deploy) ──→ T3.2 (Trino Svc) ──┤            │
T3.5 (Arroyo Deploy) ──→ T3.6 (Arroyo Svc) ────────────────────────────┤            │
T3.7 (Arroyo ConfigMap) ───────────────────────────────────────────────┤            │
                                                                       ▼            │
T3.4 (ArgoCD iceberg-query) ──→ PR 3 MERGED ◄── T3.8 (ArgoCD arroyo)               │
                                                                       │            │
                                                                       ▼            │
                                                              T4.1–T4.8 (Verify) ◄──┘
```

---

## Effort Summary

| Phase | Tasks | Git-managed | Manual | Total Effort |
|-------|-------|-------------|--------|--------------|
| Phase 1: Tansu | 6 (T1.1–T1.6) | 6 | 0 | ~M |
| Phase 2: OTel Collector | 6 (T2.1–T2.6) | 6 | 0 | ~L |
| Phase 3: Query Layer | 8 (T3.1–T3.8) | 8 | 0 | ~M |
| Phase 4: Verification | 8 (T4.1–T4.8) | 1 (T4.8) | 7 | ~L |
| **Total** | **28** | **21** | **7** | **~XL** |
