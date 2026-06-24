# Proposal: Log Normalization — OTel + Tansu + Iceberg

**Change ID**: `log-normalization`  
**Status**: Proposal  
**Date**: 2026-06-24  
**Owner**: manudiv16  
**Stacks affected**: `infrastructure`, `apps`, `argocd`

---

## 1. Intent

Introduce a parallel log ingestion pipeline using the OpenTelemetry Collector DaemonSet (with `filelog` receiver), Tansu as a Kafka-compatible streaming broker, and Apache Iceberg as the unified table format on S3. The pipeline must **normalize heterogeneous log formats** (JSON, plain text, multiline stack traces, key=value, glog) into the **OpenTelemetry Log Data Model** so that all logs become queryable through a single schema.

The existing FluentBit → Parquet pipeline remains operational during validation. Both pipelines coexist and read the same container log files.

---

## 2. Motivation

### 2.1 Why migrate from FluentBit→Parquet to OTel→Tansu→Iceberg?

The current pipeline works but has structural limitations:

| Concern | FluentBit → Parquet | OTel → Tansu → Iceberg |
|---|---|---|
| **Protocol** | FluentBit-native forwarding | OTLP (OpenTelemetry standard) |
| **Schema** | Ad-hoc Parquet column layout | OpenTelemetry Log Data Model (standardized) |
| **Streaming** | None — direct S3 writes | Kafka-compatible event stream (Tansu) |
| **Table format** | Parquet files (flat) | Iceberg (time travel, schema evolution, ACID) |
| **Ecosystem** | FluentBit plugin ecosystem | OTel + Trino/DuckDB/Arroyo ecosystem |
| **Observability** | Logs only | Unified OTel for logs + metrics (existing OTel infra) |

The migration unlocks:
- **Schema evolution**: Iceberg supports schema changes without rewriting existing data — critical for a homelab where new apps introduce new log formats over time.
- **Time travel**: Debug historical log states without managing file-level snapshots.
- **Unified protocol**: OTLP is the industry standard for telemetry, enabling the same collector to eventually handle traces alongside logs and metrics.
- **Streaming layer**: Tansu provides replayability, consumer groups, and decoupled processing — enabling downstream consumers (Arroyo streaming SQL, Trino OLAP queries) to work independently of ingestion.

### 2.2 Why now?

The homelab already runs an OTel Collector DaemonSet for metrics. Adding log collection leverages the same operational knowledge, the same ArgoCD deployment pattern, and the same cluster infrastructure. Running both pipelines in parallel eliminates migration risk — we can validate the new pipeline against production-quality log data before decommissioning the old one.

---

## 3. Current State

```
┌─────────────────────────────────────────────┐
│               Kubernetes Nodes              │
│                                             │
│  /var/log/containers/*.log                  │
│         │                                   │
│         ▼                                   │
│  ┌──────────────┐     ┌──────────────────┐  │
│  │  FluentBit   │     │  FluentBit       │  │
│  │  DaemonSet   │     │  ClusterEvents   │  │
│  │  (log fwd)   │     │  (k8s events)    │  │
│  └──────┬───────┘     └───────┬──────────┘  │
│         │                     │             │
└─────────┼─────────────────────┼─────────────┘
          │                     │
          ▼                     ▼
┌─────────────────────────────────────────────┐
│         FluentBit Aggregator                │
│         (single instance)                   │
│         - Parses container logs             │
│         - Collects k8s events               │
│         - Buffers & forwards                │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│         S3 (Scaleway)                       │
│         Parquet files                       │
│         /year/month/day/*.parquet           │
└─────────────────────────────────────────────┘

Separate: OTel Collector DaemonSet (metrics only)
  hostmetrics + kubeletstats → OTel Gateway → Prometheus remote write
```

Key observations:
- FluentBit handles both log forwarding and Kubernetes event collection in the aggregator.
- Parquet files are written directly to S3 with no streaming layer.
- Log normalization is implicit — FluentBit's parser plugins handle JSON parsing but there is no explicit enforcement of a unified schema.
- The OTel Collector DaemonSet already exists but handles **metrics only**.

---

## 4. Target State

```
┌──────────────────────────────────────────────────────────────────┐
│                    Kubernetes Nodes                              │
│                                                                  │
│  /var/log/containers/*.log                                       │
│         │                                                        │
│         ├────────────────────────────┐                           │
│         │                            │                           │
│         ▼                            ▼                           │
│  ┌──────────────┐        ┌──────────────────────┐                │
│  │  FluentBit   │        │  OTel Collector      │                │
│  │  DaemonSet   │        │  DaemonSet (existing)│                │
│  │  (existing)  │        │  - metrics pipeline  │                │
│  │              │        │  - logs pipeline (+) │                │
│  └──────┬───────┘        └──────────┬───────────┘                │
│         │                           │                            │
└─────────┼───────────────────────────┼────────────────────────────┘
          │                           │
          ▼                           ▼
┌──────────────────┐   ┌──────────────────────────────────────────┐
│  FluentBit       │   │  OTel Collector processing (logs):        │
│  Aggregator      │   │  - filelog receiver                       │
│  (existing)      │   │  - k8sattributes processor (shared)       │
│                  │   │  - transform processor (normalize)        │
└────────┬─────────┘   │  - batch + retry                          │
         │             └────────────────────┬──────────────────────┘
         ▼                                  │
┌──────────────────┐                        ▼
│  S3 (Scaleway)   │             ┌───────────────────────┐
│  Parquet (legacy)│             │  Tansu Broker         │
│                  │             │  (Kafka-compatible)    │
└──────────────────┘             │  - OTLP consumer       │
                                 │  - Iceberg writer      │
                                 │    (nativo, sin k2i)   │
                                 └───────────┬───────────┘
                                             │
                            ┌────────────────┼────────────────┐
                            ▼                ▼                ▼
                     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
                     │  Iceberg     │ │  Arroyo      │ │  Direct S3   │
                     │  Tables      │ │  (streaming  │ │  (Iceberg    │
                     │  on S3       │ │   SQL)       │ │   files)     │
                     └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
                                           │
                          ┌────────────────┼────────────────┐
                          ▼                ▼                ▼
                   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
                   │  Iceberg     │ │  Arroyo      │ │  Direct S3   │
                   │  Tables      │ │  (streaming  │ │  (Iceberg    │
                   │  on S3       │ │   SQL)       │ │   files)     │
                   └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
                          │                │                │
                          ▼                ▼                ▼
                   ┌──────────────────────────────────────────────┐
                   │  Query Layer                                 │
                   │  - DuckDB (ad-hoc, local)                    │
                   │  - Trino (distributed SQL)                   │
                   │  - Arroyo (streaming SQL on Tansu topics)    │
                   └──────────────────────────────────────────────┘
```

### 4.1 Coexistence model

| Aspect | FluentBit pipeline (old) | OTel pipeline (new) |
|---|---|---|
| **Reads** | `/var/log/containers/*.log` | `/var/log/containers/*.log` |
| **Conflict?** | No — both are read-only consumers | No — both are read-only consumers |
| **Output** | Parquet → S3 | Iceberg → S3 (via Tansu) |
| **Status** | Active, unchanged | Active, parallel |
| **Decommission** | After validation period | Becomes primary |

Both pipelines consume the same source files simultaneously. There is no file locking or mutation of source logs — only reads. FluentBit's aggregation behavior and OTel's `filelog` receiver both support independent file tailing with separate checkpoint/offset tracking.

---

## 5. Core Problem: Log Normalization

### 5.1 The challenge

Different applications produce fundamentally different log formats. The homelab runs a mix of:

| Format | Example sources | Characteristics |
|---|---|---|
| **JSON structured** | Custom apps, some K8s components | Already structured; need field mapping |
| **Plain text** | nginx access/error logs, system daemons | Unstructured; need pattern-based parsing |
| **Multiline** | Java stack traces, Python tracebacks, Go panic dumps | Span multiple lines; need reassembly |
| **Key=Value** | Some Go services, systemd journal | Semi-structured; need kv extraction |
| **glog** | Kubernetes components, Go services | Timestamp prefix + severity + file:line header |

The goal is to normalize **all** of these into the **OpenTelemetry Log Data Model**:

```yaml
# OTel Log Data Model (canonical fields)
timestamp:        # Unix nano — when the event occurred
observed_timestamp: # Unix nano — when the collector saw it
severity_number:  # 1-24 (TRACE to FATAL)
severity_text:    # "INFO", "ERROR", etc.
body:             # string — the original log message or structured body
attributes:       # map<string, any>
  k8s.namespace.name:
  k8s.pod.name:
  k8s.container.name:
  k8s.node.name:
  service.name:
  service.namespace:
  # optional:
  trace_id:
  span_id:
resource:         # describes the entity that produced the log
  service.name:
  k8s.*:
```

### 5.2 Normalization strategy (two-pass)

**First pass — OTel Collector (inline, low-latency):**
- `filelog` receiver reads container log files
- `k8sattributes` processor enriches with Kubernetes metadata
- `transform` processor applies format-specific parsing rules:
  - JSON body → parse and flatten into attributes
  - glog pattern → extract severity and timestamp
  - Key=value → extract into attributes map
- Basic multiline handling via `filelog` receiver's `multiline` configuration
- Output: normalized OTLP logs to Tansu

**Second pass — Arroyo (streaming SQL, async):**
- Consumes from Tansu topics
- Applies complex normalization that the `transform` processor cannot handle:
  - Multiline stack trace reassembly (if first-pass was insufficient)
  - Cross-log correlation (joining related events)
  - Schema enrichment (looking up service metadata)
- Writes normalized results back to Iceberg tables

This two-pass approach keeps the collector lightweight (fast path for 80% of logs) while reserving heavy processing for Arroyo (complex 20%).

---

## 6. Scope

### In scope
- **ALL namespaces**: Every pod in every namespace is covered by the OTel Collector DaemonSet
- **ALL log formats**: JSON, plain text, multiline, key=value, glog
- **Kubernetes metadata enrichment**: k8sattributes processor adds namespace, pod, container, node labels
- **Tansu deployment**: Self-hosted Kafka-compatible broker in the homelab cluster
- **Iceberg tables on S3**: Tansu writes directly to Iceberg format on existing Scaleway S3
- **Query layer**: DuckDB (ad-hoc), Trino (distributed), Arroyo (streaming SQL)
- **ArgoCD integration**: All new components deployed as ArgoCD Applications under the App of Apps pattern
- **Parallel operation**: Old FluentBit pipeline remains active and unchanged

### Out of scope (Non-goals)
- **Historical data migration**: Existing Parquet files in S3 are left as-is. No backfill, no conversion to Iceberg.
- **Metrics pipeline replacement**: The existing OTel → Prometheus metrics pipeline stays unchanged.
- **Old pipeline removal**: FluentBit pipeline is NOT decommissioned until the new pipeline is validated.
- **Trace ingestion**: This proposal covers logs only. Traces may be added in a future change.
- **Alerting / log-based alerts**: Not part of this change; query layer enables future alerting.

---

## 7. Proposed Approach

### 7.1 OTel Collector DaemonSet (existente, extendido)

Se **extiende el OTel Collector DaemonSet existente** (`collector-daemonset` en `infrastructure/monitoring/otel-collector/`) añadiendo un pipeline `logs` al mismo recurso OpenTelemetryCollector. No se despliega un DaemonSet nuevo — se reutiliza el que ya está recolectando métricas (hostmetrics, kubeletstats).

```yaml
# High-level OTel Collector pipeline
receivers:
  filelog:
    include:
      - /var/log/containers/*.log
    multiline:
      line_start_pattern: <TBD — format-dependent>
    operators:
      # Container runtime parser (CRI-O / containerd)
      - type: regex
        id: container-parser
        # ... parse log line into timestamp, stream, log body
      # JSON parser for structured logs
      - type: json_parser
        id: json-parser
        parse_from: attributes.log
        # ... only fires if body is valid JSON

processors:
  k8sattributes:
    # Enrich with pod, namespace, container, node metadata
  transform:
    # Normalize severity, parse formats, map attributes
    log_statements:
      - context: log
        statements:
          - set(severity_text, ...) # normalize severity labels
          - set(attributes["service.name"], ...)

exporters:
  otlp:
    endpoint: tansu-broker:4317
    tls:
      insecure: true # internal cluster traffic; TLS in a future change
```

### 7.2 Tansu Broker

Deploy Tansu as a StatefulSet with persistent storage. **Tansu escribe directamente a Iceberg de forma nativa** — no necesita k2i ni Kafka Connect ni componentes adicionales. Soporta REST catalog y Hadoop catalog para Iceberg.

```
Tansu configuration:
  --storage-engine=postgres://...     # o sqlite para empezar
  --iceberg-catalog=<catalog-type>    # REST, Hadoop, etc.
  --iceberg-namespace=logs
  --kafka-listeners=PLAINTEXT://:9092
```

Tansu acts as:
- OTLP consumer endpoint (recibe logs del OTel Collector)
- Kafka-compatible broker (Arroyo, Trino, DuckDB consumen como topics)
- Iceberg writer nativo (vía flag `--iceberg-catalog`, escribe tablas Iceberg directamente en S3)

### 7.3 Query Layer

| Tool | Role | Deployment |
|---|---|---|
| **DuckDB** | Ad-hoc queries, local exploration, validation | CLI / pod exec, reads Iceberg directly from S3 |
| **Trino** | Distributed SQL, multi-source queries | Deployment + Service, queries Iceberg tables |
| **Arroyo** | Streaming SQL on Tansu topics | Deployment, second-pass normalization |

### 7.4 ArgoCD Integration

New ArgoCD Applications in `apps/`:

| Application | Points to | Deploys |
|---|---|---|
| `tansu` | `infrastructure/tansu/` | Tansu StatefulSet, Service, PVC |
| `otel-collector` | *(existente, se modifica in-place)* | Se actualiza el ConfigMap del DaemonSet existente |
| `iceberg-query` | `infrastructure/iceberg-query/` | Trino, DuckDB (optional) |
| `arroyo` | `infrastructure/arroyo/` | Arroyo deployment |

The existing `apps/` App of Apps root Application will pick these up automatically via `recurse: true`.

---

## 8. Parallelism Strategy

Both pipelines read from `/var/log/containers/*.log` simultaneously:

- **No file locking conflicts**: Container log files are append-only. Both FluentBit and OTel's `filelog` receiver tail files independently.
- **Separate offset tracking**: FluentBit uses its own database for file positions; OTel uses its own checkpoint mechanism. No shared state.
- **Independent buffering**: Each pipeline has its own memory/disk buffer. A failure in one does not affect the other.
- **Resource isolation**: OTel Collector DaemonSet runs in its own namespace (or shares the existing OTel namespace). Resource requests/limits are set independently.
- **Validation workflow**: Compare log counts, field coverage, and data quality between the two pipelines over a multi-day observation period.

---

## 9. Key Risks

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| **Multiline log handling** | Stack traces split into separate log entries, losing context | High | OTel `filelog` receiver supports `multiline` config with `line_start_pattern`. Need careful regex tuning per format. Second-pass Arroyo can reassemble as fallback. |
| **OTel transform processor performance** | High CPU/memory on collector at scale (hundreds of pods) | Medium | Benchmark with production log volume. Use batch processor. Consider reducing transform complexity for first-pass. |
| **Tansu learning curve** | Tansu is a relatively new self-hosted broker; limited homelab deployment experience | Medium | Start with SQLite backend for simplicity. Evaluate PostgreSQL backend for production readiness. Document deployment steps. |
| **Iceberg catalog choice** | Wrong catalog type complicates schema evolution and query compatibility | Medium | Start with REST catalog or Hadoop catalog. Evaluate against Trino/DuckDB compatibility matrix. |
| **Resource contention** | Running two log pipelines doubles I/O on `/var/log/containers/` reads | Low | File reads are sequential and cached by OS. Memory/CPU is the real constraint — set appropriate resource limits. |
| **Schema drift** | New log formats appear over time that don't match existing parsers | Medium | Design `transform` processor rules to be incremental. Arroyo second-pass handles unknown formats gracefully by preserving raw body. |

---

## 10. Rollback

Rollback is straightforward due to the parallel architecture:

1. **Disable OTel log collection**: Remove or scale down the OTel Collector DaemonSet for logs (or remove the `filelog` receiver from the existing OTel Collector if merged).
2. **Stop Tansu**: Scale down Tansu StatefulSet to 0 replicas.
3. **Clean up Iceberg tables**: Optional — leave Iceberg tables on S3 for historical reference, or delete them.
4. **ArgoCD sync**: Remove new ArgoCD Applications from `apps/` directory. The App of Apps pattern with `prune: true` will clean up cluster resources.

The FluentBit pipeline is **never modified** during rollback — it continues serving as the single source of truth.

---

## 11. Success Criteria

The change is successful when:

1. **Coverage**: OTel Collector DaemonSet collects logs from **100% of running pods** across all namespaces.
2. **Normalization**: At least **90% of log entries** are parsed into the OTel Log Data Model with correct `severity_number`, `severity_text`, `body`, and `k8s.*` attributes populated.
3. **Multiline handling**: Stack traces and multiline exceptions are reassembled into **single log entries** (not split across multiple entries).
4. **Iceberg writes**: Tansu successfully writes Iceberg tables to S3, and the tables are **queryable by both DuckDB and Trino**.
5. **Parity validation**: Log count in Iceberg matches log count in FluentBit Parquet output within **±5%** over a 24-hour observation window.
6. **Performance**: OTel Collector DaemonSet uses **< 200Mi memory** and **< 250m CPU** per node under production load.
7. **ArgoCD deployment**: All new components deploy via ArgoCD with **automated sync** and **zero manual kubectl apply** steps.

---

## 12. Open Questions (for Spec Phase)

The following decisions are deferred to the Spec and Design phases:

| # | Question | Why it matters |
|---|---|---|
| 1 | **Which OTel operators for each log format?** The `filelog` receiver supports multiple operators (json_parser, regex_parser, key_value_parser, etc.). Which ones, in what order, and with what fallback logic? | Determines the actual `transform` processor configuration and parsing reliability. |
| 2 | **Multiline detection strategy** — regex-based `line_start_pattern`, timeout-based, or language-specific heuristics? | Incorrect multiline handling is the highest risk. Need a strategy that covers Java, Python, and Go stack traces. |
| 3 | **Tansu deployment mode** — SQLite backend (simplest), PostgreSQL backend (production-grade), or direct S3 writes without a persistent catalog? | Affects durability, recovery, and query compatibility. SQLite is easiest to start with but may not survive node failures. |
| 4 | **Iceberg catalog type** — Hadoop catalog, REST catalog, or custom? Must be compatible with both Trino and DuckDB. | Catalog choice determines schema evolution capabilities and query engine support. |
| 5 | **Schema evolution strategy** — how do we handle new attributes appearing in logs over time? Iceberg supports schema evolution, but the OTel Collector config must also adapt. | Without a strategy, new log formats will either break ingestion or lose data. |
| 6 | ~~Should we extend the existing OTel Collector DaemonSet (metrics) or deploy a separate logs-specific DaemonSet?~~ | ✅ **Decidido**: se extiende el DaemonSet existente. Se ajustarán los recursos (CPU/memoria) para absorber el pipeline de logs. |
| 7 | **TLS for internal OTLP traffic** — enable from day one or defer? | Security vs. complexity. Internal cluster traffic is currently unencrypted for metrics. Se decide en Design phase. |

---

## 13. Affected Areas

| Area | Change | Stack |
|---|---|---|
| `infrastructure/monitoring/otel-collector/collector.yaml` | **Modified** — se añade pipeline `logs` al DaemonSet existente | `infrastructure` |
| `infrastructure/tansu/` | **New** — Tansu StatefulSet, Service, PVC, ConfigMap | `infrastructure` |
| `infrastructure/iceberg-query/` | **New** — Trino Deployment/Service, DuckDB (optional) | `infrastructure` |
| `infrastructure/arroyo/` | **New** — Arroyo Deployment, streaming SQL configs | `infrastructure` |
| `apps/tansu.yaml` | **New** — ArgoCD Application definition | `apps` |
| `apps/iceberg-query.yaml` | **New** — ArgoCD Application definition | `apps` |
| `apps/arroyo.yaml` | **New** — ArgoCD Application definition | `apps` |
| Existing FluentBit manifests | **No change** — pipeline remains active | — |
| Existing monitoring namespace / OTel operator | **No change** — ya existe | — |
