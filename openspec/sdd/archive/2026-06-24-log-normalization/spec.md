# Log Normalization Specification

## Purpose

Extend the existing OTel Collector DaemonSet with a logs pipeline that normalizes heterogeneous container log formats (JSON, plain text, multiline, key=value, glog) into the OpenTelemetry Log Data Model. Deliver normalized logs to Apache Iceberg via Tansu (Kafka-compatible broker) and expose them through a query layer (Arroyo, Trino, DuckDB). The existing FluentBit → Parquet pipeline remains operational and unmodified throughout.

---

## Requirements

### Requirement: OTel Collector Logs Pipeline

The system MUST extend the existing `collector-daemonset` OpenTelemetryCollector CR in namespace `monitoring` by adding a `logs` pipeline alongside the existing `metrics` pipeline. A separate DaemonSet MUST NOT be deployed. The logs pipeline MUST read all container logs from every namespace using the `filelog` receiver on `/var/log/containers/*.log`.

#### Scenario: Logs pipeline running on all nodes

- GIVEN the `collector-daemonset` OpenTelemetryCollector CR is updated with a `logs` pipeline
- WHEN ArgoCD syncs the change
- THEN every node MUST have one collector pod running with both `metrics` and `logs` pipelines active
- AND the collector pods MUST NOT be OOMKilled within the first 30 minutes of operation

#### Scenario: All namespaces covered

- GIVEN a pod in any namespace writes logs to stdout/stderr
- WHEN the filelog receiver tails `/var/log/containers/*.log`
- THEN logs from that pod MUST be collected regardless of namespace

#### Scenario: Existing metrics pipeline unaffected

- GIVEN the metrics pipeline (hostmetrics, kubeletstats) is operational
- WHEN the logs pipeline is added to the same collector CR
- THEN metrics collection interval, processors, and exporters MUST remain identical to their current configuration
- AND metrics MUST continue flowing to the OTel Gateway at `otel-gateway-collector:4317`

---

### Requirement: Container Runtime Log Parsing

The system MUST parse CRI-format container log lines (containerd/k3s) into structured fields. Each line from `/var/log/containers/*.log` follows the CRI log format: `<timestamp> <stream> <logtag> <log>`.

#### Scenario: CRI log line parsed correctly

- GIVEN a container log line: `2026-06-24T10:15:30.123456789Z stdout F {"level":"info","msg":"started"}`
- WHEN the filelog receiver's `container` operator processes the line
- THEN the `timestamp` field MUST be `2026-06-24T10:15:30.123456789Z` (parsed to Unix nanos)
- AND `attributes["log.iostream"]` MUST be `stdout`
- AND the log body MUST be `{"level":"info","msg":"started"}`

#### Scenario: Stderr stream preserved

- GIVEN a container log line with `stderr` stream
- WHEN the filelog receiver parses it
- THEN `attributes["log.iostream"]` MUST be `stderr`
- AND this attribute MUST be preserved through all pipeline stages

---

### Requirement: Kubernetes Metadata Enrichment

The system MUST enrich every log record with Kubernetes metadata using the `k8sattributes` processor. The processor MUST populate: `k8s.namespace.name`, `k8s.pod.name`, `k8s.container.name`, `k8s.node.name`, and `k8s.deployment.name` (when applicable).

#### Scenario: Pod metadata attached to log record

- GIVEN a log from pod `my-app-abc123` in namespace `production`, container `web`
- WHEN the `k8sattributes` processor runs
- THEN `resource["k8s.namespace.name"]` MUST be `production`
- AND `resource["k8s.pod.name"]` MUST be `my-app-abc123`
- AND `resource["k8s.container.name"]` MUST be `web`

#### Scenario: Node name populated

- GIVEN a log collected by the DaemonSet pod on node `k3s-worker-1`
- WHEN the `k8sattributes` processor runs
- THEN `resource["k8s.node.name"]` MUST be `k3s-worker-1`

#### Scenario: Service name inferred

- GIVEN a pod with label `app.kubernetes.io/name=my-service`
- WHEN the `k8sattributes` processor extracts labels
- THEN `resource["service.name"]` SHOULD be set to `my-service` if the label exists

---

### Requirement: Format Detection and Routing

The system MUST detect the log format of each record and apply format-specific normalization. Detection MUST occur in the `transform` processor using OTTL statements. The system MUST handle these formats in priority order:

1. **JSON structured** — body starts with `{`
2. **glog** — body matches `^([IWDEF])(\d{4} \d{2}:\d{2}:\d{2}\.\d+ +\d+ .+:\d+])(.*)$` (e.g., `I0624 10:15:30.123456   12345 server.go:42] message`)
3. **Key=Value** — body contains `key=value` pairs separated by spaces
4. **Plain text** — fallback; no structured fields extracted

The system MUST set `attributes["log.format"]` to the detected format name (`json`, `glog`, `keyvalue`, `text`).

#### Scenario: JSON log detected and parsed

- GIVEN a log body `{"level":"info","msg":"request completed","duration_ms":42}`
- WHEN the transform processor processes it
- THEN `attributes["log.format"]` MUST be `json`
- AND `attributes["log.level"]` MUST be `info`
- AND `attributes["log.msg"]` MUST be `request completed`
- AND `attributes["log.duration_ms"]` MUST be `42`

#### Scenario: glog line detected

- GIVEN a log body `I0624 10:15:30.123456   12345 server.go:42] Listening on :8080`
- WHEN the transform processor processes it
- THEN `attributes["log.format"]` MUST be `glog`
- AND `severity_text` MUST be `INFO`
- AND `severity_number` MUST be `9`
- AND `attributes["log.source_file"]` MUST be `server.go`
- AND `attributes["log.source_line"]` MUST be `42`

#### Scenario: Key=Value log detected

- GIVEN a log body `ts=2026-06-24T10:15:30Z level=info msg="ready" port=8080`
- WHEN the transform processor processes it
- THEN `attributes["log.format"]` MUST be `keyvalue`
- AND `attributes["level"]` MUST be `info`
- AND `attributes["msg"]` MUST be `ready`
- AND `attributes["port"]` MUST be `8080`

#### Scenario: Plain text fallback

- GIVEN a log body `Connection from 10.0.0.5 port 22 accepted`
- WHEN the transform processor cannot detect JSON, glog, or key=value
- THEN `attributes["log.format"]` MUST be `text`
- AND the body MUST be preserved unchanged as `body`

---

### Requirement: Severity Normalization

The system MUST map all detected severity indicators to the OpenTelemetry severity number/text scheme. The mapping MUST cover:

| Source Value | severity_text | severity_number |
|---|---|---|
| `trace` / `TRACE` / `T` (glog) | TRACE | 1 |
| `debug` / `DEBUG` / `D` (glog) | DEBUG | 5 |
| `info` / `INFO` / `I` (glog) / no severity | INFO | 9 |
| `warn` / `warning` / `WARN` / `WARNING` / `W` (glog) | WARN | 13 |
| `error` / `ERROR` / `E` (glog) | ERROR | 17 |
| `fatal` / `panic` / `FATAL` / `PANIC` / `F` (glog) | FATAL | 21 |

The system MUST normalize severity case-insensitively. When no severity is detectable, the system MUST default to `severity_number=9` (INFO) and `severity_text=INFO`.

#### Scenario: JSON log with level field

- GIVEN a JSON log body `{"level":"error","msg":"connection refused"}`
- WHEN the transform processor normalizes severity
- THEN `severity_text` MUST be `ERROR`
- AND `severity_number` MUST be `17`

#### Scenario: glog fatal line

- GIVEN a glog body `F0624 10:15:30.123456   12345 main.go:10] Fatal init error`
- WHEN the transform processor normalizes severity
- THEN `severity_text` MUST be `FATAL`
- AND `severity_number` MUST be `21`

#### Scenario: Text log with no detectable severity

- GIVEN a plain text log body `Connection from 10.0.0.5 port 22 accepted`
- WHEN the transform processor cannot detect severity
- THEN `severity_text` MUST default to `INFO`
- AND `severity_number` MUST default to `9`

#### Scenario: Case-insensitive severity matching

- GIVEN a JSON log body `{"level":"Warning","msg":"disk almost full"}`
- WHEN the transform processor normalizes severity
- THEN `severity_text` MUST be `WARN`
- AND `severity_number` MUST be `13`

---

### Requirement: Multiline Log Reassembly

The system MUST reassemble multiline log entries (stack traces, panics, exception blocks) into a single log record. The filelog receiver MUST use `multiline` configuration with format-specific `line_start_pattern` values.

The system MUST handle these multiline patterns:

| Language/Framework | line_start_pattern | Example |
|---|---|---|
| Java (Spring, Log4j, Logback) | `^\d{4}-\d{2}-\d{2}` (ISO date at line start) | `2026-06-24 10:15:30.123 ERROR ...` followed by `	at com.example.App.method(App.java:42)` |
| Python (traceback) | `^(Traceback\|  File\|[A-Z]\w+Error\|[A-Z]\w+Exception)` | `Traceback (most recent call last):` followed by `  File "app.py", line 10` |
| Go (panic) | `^(panic\|goroutine \d+ \[)` | `goroutine 1 [running]:` followed by `main.main()` |
| Node.js (stack trace) | `^\s+at\s` (lines starting with `  at`) | `Error: something failed` followed by `    at Object.<anonymous> (app.js:10:15)` |

The system MUST apply a multiline `flush_after` timeout (default: 5 seconds) to avoid blocking indefinitely on unterminated multiline blocks.

#### Scenario: Java stack trace reassembled

- GIVEN a Java exception spanning 5 log lines:
  ```
  2026-06-24 10:15:30.123 ERROR c.e.MyController - Request failed
  java.lang.NullPointerException: null
  	at com.example.MyController.handle(MyController.java:42)
  	at org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:897)
  	... 42 more
  ```
- WHEN the filelog receiver processes these lines
- THEN all 5 lines MUST be combined into a single log record
- AND the body MUST contain the full stack trace as a single string
- AND the timestamp MUST be `2026-06-24T10:15:30.123`

#### Scenario: Python traceback reassembled

- GIVEN a Python traceback spanning 4 log lines:
  ```
  Traceback (most recent call last):
    File "/app/main.py", line 10, in process
      result = data["key"]
  KeyError: 'key'
  ```
- WHEN the filelog receiver processes these lines
- THEN all 4 lines MUST be combined into a single log record

#### Scenario: Multiline flush timeout

- GIVEN a multiline block that is never terminated (e.g., truncated log)
- WHEN 5 seconds elapse after the last line of the block
- THEN the filelog receiver MUST flush the accumulated lines as a single log record
- AND the record MUST NOT be held indefinitely

---

### Requirement: Timestamp Extraction and Normalization

The system MUST extract the timestamp from each log record and normalize it to Unix nanoseconds in the OTel `time_unix_nano` field.

- **CRI timestamp**: Already parsed by the `container` operator from the CRI prefix.
- **JSON timestamp**: Extract from `timestamp`, `time`, `ts`, `@timestamp`, or `datetime` fields in the JSON body.
- **glog timestamp**: Extract from the `MMDD HH:MM:SS.microseconds` prefix.
- **Key=Value timestamp**: Extract from `ts`, `time`, or `timestamp` keys.
- **Fallback**: Use `observed_timestamp` (time the collector read the line) as `timestamp`.

#### Scenario: JSON timestamp field used

- GIVEN a JSON log body `{"timestamp":"2026-06-24T10:15:30.123456Z","level":"info","msg":"ok"}`
- WHEN the transform processor extracts the timestamp
- THEN `time_unix_nano` MUST be set to the nanosecond equivalent of `2026-06-24T10:15:30.123456Z`

#### Scenario: glog timestamp extracted

- GIVEN a glog body `I0624 10:15:30.123456   12345 server.go:42] message`
- WHEN the transform processor extracts the timestamp
- THEN `time_unix_nano` MUST be derived from `06-24 10:15:30.123456` (year inferred from the current date or CRI prefix)

#### Scenario: Fallback to observed timestamp

- GIVEN a plain text log body with no extractable timestamp
- WHEN the transform processor cannot find a timestamp field
- THEN `time_unix_nano` MUST equal the CRI-parsed timestamp or `observed_timestamp`

---

### Requirement: Cluster Metadata Attributes

The system MUST set the `cluster.name` resource attribute to `homelab` on all log records, consistent with the existing metrics pipeline's `resource/cluster` processor.

#### Scenario: Cluster name on log records

- GIVEN a log record from any pod in the cluster
- WHEN the `resource/cluster` processor runs
- THEN `resource["cluster.name"]` MUST be `homelab`

---

### Requirement: OTLP Export to Tansu

The system MUST export normalized log records via OTLP gRPC to the Tansu broker endpoint. The exporter MUST be named `otlp/tansu` and MUST connect to a dedicated Tansu OTLP endpoint within the cluster.

#### Scenario: Logs exported to Tansu

- GIVEN the logs pipeline processes a batch of log records
- WHEN the batch processor flushes (timeout or size threshold)
- THEN the OTLP exporter MUST send the batch to Tansu via gRPC
- AND the exporter MUST retry on transient failures with exponential backoff

#### Scenario: Export failure isolation

- GIVEN the Tansu endpoint is temporarily unavailable
- WHEN the OTLP exporter fails to send a batch
- THEN the failure MUST NOT affect the metrics pipeline
- AND the exporter MUST retry up to the configured `max_elapsed_time` before dropping

---

### Requirement: Tansu Broker Deployment

The system MUST deploy Tansu as a StatefulSet in the `monitoring` namespace. Tansu MUST receive OTLP logs from the collector, act as a Kafka-compatible broker for downstream consumers, and write Iceberg tables natively to S3 (Scaleway).

#### Scenario: Tansu accepting OTLP logs

- GIVEN Tansu is deployed and running
- WHEN the OTel Collector exports logs via OTLP
- THEN Tansu MUST accept the OTLP gRPC connection
- AND log records MUST be persisted in Tansu's internal storage

#### Scenario: Kafka-compatible topic consumption

- GIVEN Tansu has ingested logs into a topic
- WHEN Arroyo or Trino connects as a Kafka consumer
- THEN they MUST be able to consume log records from Tansu topics

#### Scenario: Iceberg table writes

- GIVEN Tansu receives log records
- WHEN records are flushed to storage
- THEN Tansu MUST write Iceberg tables to S3 at the configured warehouse path
- AND tables MUST be readable by Trino and DuckDB

#### Scenario: Tansu persistence recovery

- GIVEN Tansu is restarted (pod eviction, node drain)
- WHEN Tansu comes back up
- THEN it MUST recover its internal state from the persistent storage backend
- AND no log records that were acknowledged before the restart MUST be lost

---

### Requirement: Iceberg Schema for Logs

The system MUST define an Iceberg table schema that maps directly to the OpenTelemetry Log Data Model. The table MUST support schema evolution for future attribute additions.

The primary table MUST be named `otel_logs` and MUST contain these columns:

| Column | Iceberg Type | OTel Source Field | Nullable |
|---|---|---|---|
| `timestamp` | `timestamptz` (nanos) | `time_unix_nano` | No |
| `observed_timestamp` | `timestamptz` (nanos) | `observed_time_unix_nano` | No |
| `severity_number` | `int` | `severity_number` | No |
| `severity_text` | `string` | `severity_text` | No |
| `body` | `string` | `body` | No |
| `trace_id` | `string` | `trace_id` | Yes |
| `span_id` | `string` | `span_id` | Yes |
| `k8s_namespace` | `string` | `resource["k8s.namespace.name"]` | No |
| `k8s_pod` | `string` | `resource["k8s.pod.name"]` | No |
| `k8s_container` | `string` | `resource["k8s.container.name"]` | No |
| `k8s_node` | `string` | `resource["k8s.node.name"]` | No |
| `k8s_deployment` | `string` | `resource["k8s.deployment.name"]` | Yes |
| `service_name` | `string` | `resource["service.name"]` | Yes |
| `cluster_name` | `string` | `resource["cluster.name"]` | No |
| `log_format` | `string` | `attributes["log.format"]` | No |
| `attributes` | `map<string, string>` | remaining `attributes` | Yes |

The table MUST be partitioned by `k8s_namespace` and day-granularity on `timestamp` (Iceberg transform: `days(timestamp)`).

#### Scenario: Query by namespace and time range

- GIVEN logs are stored in the `otel_logs` Iceberg table
- WHEN a query filters by `k8s_namespace = 'production' AND timestamp > now() - INTERVAL 1 HOUR`
- THEN the query engine MUST prune partitions unrelated to `production` and the last hour
- AND return matching log records

#### Scenario: Schema evolution adds new column

- GIVEN the `otel_logs` table exists with the current schema
- WHEN a new attribute `log.correlation_id` is promoted to a top-level column
- THEN Iceberg MUST support adding the column without rewriting existing data files
- AND queries for the new column on old data MUST return NULL

---

### Requirement: Arroyo Streaming SQL

The system MUST deploy Arroyo as a Deployment in the `monitoring` namespace. Arroyo MUST consume from Tansu topics, apply second-pass normalization (complex format detection, cross-log correlation), and write results back to Iceberg.

#### Scenario: Arroyo consuming from Tansu

- GIVEN Arroyo is deployed and configured with Tansu as the Kafka source
- WHEN new log records arrive in Tansu
- THEN Arroyo MUST consume them in near-real-time (within 30 seconds)

#### Scenario: Second-pass normalization

- GIVEN a log record that the first pass (OTel Collector) tagged as `log.format=text` but is actually a multiline entry that was split
- WHEN Arroyo's streaming SQL job processes it
- THEN Arroyo SHOULD reassemble the split lines and update the record in the output table

#### Scenario: Arroyo writes to Iceberg

- GIVEN Arroyo processes a window of log records
- WHEN the window flushes
- THEN Arroyo MUST write the output to the `otel_logs` Iceberg table (or a derived table)

---

### Requirement: Trino Distributed SQL

The system MUST deploy Trino as a Deployment in the `monitoring` namespace. Trino MUST be configured with the Iceberg connector pointing to the S3 warehouse where Tansu writes tables.

#### Scenario: Trino queries Iceberg logs

- GIVEN Trino is deployed with the Iceberg connector
- WHEN a user executes `SELECT * FROM iceberg.logs.otel_logs WHERE k8s_namespace = 'monitoring' AND severity_text = 'ERROR'`
- THEN Trino MUST return matching log records from Iceberg

#### Scenario: Trino time-range queries

- GIVEN the `otel_logs` table is partitioned by timestamp
- WHEN a query filters by a time range (e.g., last hour)
- THEN Trino MUST use partition pruning to read only relevant data files

---

### Requirement: DuckDB Ad-Hoc Queries

The system MUST support DuckDB as an ad-hoc query tool for Iceberg tables. DuckDB MAY be used via CLI (pod exec or local) with the `iceberg` and `httpfs` extensions configured to read from S3.

#### Scenario: DuckDB reads Iceberg table

- GIVEN DuckDB is configured with the `iceberg` extension and S3 credentials
- WHEN a user executes `SELECT * FROM iceberg_scan('s3://logs/iceberg/otel_logs') WHERE severity_text = 'ERROR'`
- THEN DuckDB MUST return matching log records

---

### Requirement: Parallel Operation with FluentBit

The system MUST operate the OTel logs pipeline in parallel with the existing FluentBit pipeline. Both pipelines MUST read the same `/var/log/containers/*.log` files independently. The FluentBit pipeline MUST NOT be modified or decommissioned by this change.

#### Scenario: Both pipelines collecting simultaneously

- GIVEN the OTel logs pipeline is active AND the FluentBit DaemonSet is active
- WHEN a pod writes a log line to stdout
- THEN BOTH the OTel Collector AND the FluentBit DaemonSet MUST independently collect that log line
- AND neither pipeline MUST interfere with the other's file offset tracking

#### Scenario: FluentBit pipeline unchanged

- GIVEN the log-normalization change is applied
- WHEN inspecting the FluentBit DaemonSet and Aggregator configurations
- THEN they MUST be identical to their pre-change state
- AND FluentBit MUST continue writing Parquet to Scaleway S3

---

### Requirement: ArgoCD Deployment

All new components MUST be deployed as ArgoCD Application definitions in the `apps/` directory. The existing App of Apps root Application MUST automatically discover and sync them.

New ArgoCD Applications:

| Application Name | Source Path | Sync Wave |
|---|---|---|
| `otel-collector` (modified) | `infrastructure/monitoring/otel-collector/` | 2 (existing) |
| `tansu` (new) | `infrastructure/tansu/` | 3 |
| `arroyo` (new) | `infrastructure/arroyo/` | 4 |
| `iceberg-query` (new) | `infrastructure/iceberg-query/` | 4 |

All new Applications MUST use `automated` sync policy with `prune: true` and `selfHeal: true` and `CreateNamespace=true`.

#### Scenario: Tansu deployed via ArgoCD

- GIVEN a `tansu.yaml` ArgoCD Application is added to `apps/`
- WHEN ArgoCD syncs
- THEN the Tansu StatefulSet MUST be running in `monitoring` namespace
- AND the ArgoCD Application status MUST be `Synced` and `Healthy`

#### Scenario: Sync wave ordering

- GIVEN all new ArgoCD Applications are defined
- WHEN ArgoCD triggers a sync
- THEN the OTel Collector (wave 2) MUST sync before Tansu (wave 3)
- AND Tansu (wave 3) MUST sync before Arroyo and Trino (wave 4)

---

### Requirement: Resource Budget

The extended OTel Collector DaemonSet MUST operate within these resource constraints per node:

| Resource | Request | Limit |
|---|---|---|
| CPU | 150m | 500m |
| Memory | 200Mi | 600Mi |

The `memory_limiter` processor MUST be configured with `limit_mib: 500` and `spike_limit_mib: 150`.

Component resource budgets:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|---|---|---|---|---|
| Tansu | 100m | 500m | 256Mi | 512Mi |
| Arroyo | 100m | 500m | 256Mi | 512Mi |
| Trino | 250m | 1000m | 512Mi | 1Gi |

#### Scenario: Collector memory under load

- GIVEN 50 pods are running on a single node, each producing ~10 log lines/second
- WHEN the OTel Collector DaemonSet processes all logs
- THEN memory usage MUST stay below 600Mi (limit)
- AND the `memory_limiter` processor MUST drop batches before OOM

#### Scenario: Tansu resource stability

- GIVEN Tansu is receiving 1000 log records/second
- WHEN monitoring Tansu resource usage
- THEN CPU MUST stay below 500m and memory below 512Mi

---

### Requirement: Log Record Deduplication

The system MUST NOT produce duplicate log records within the OTel pipeline. The filelog receiver MUST track file offsets independently from FluentBit. If a log line is collected twice (once by each pipeline), the duplication is acceptable because the pipelines write to different storage backends.

#### Scenario: No duplicates within OTel pipeline

- GIVEN the OTel Collector restarts (pod eviction)
- WHEN the collector resumes reading from `/var/log/containers/*.log`
- THEN it MUST resume from its last checkpointed offset
- AND MUST NOT re-read already-processed log lines

---

### Requirement: Unknown Format Graceful Handling

When the transform processor cannot detect a log format, the system MUST preserve the raw body unchanged and set `attributes["log.format"]` to `text`. The record MUST NOT be dropped.

#### Scenario: Unrecognized format preserved

- GIVEN a log body that does not match JSON, glog, key=value, or known plain text patterns
- WHEN the transform processor processes it
- THEN `body` MUST contain the original text
- AND `attributes["log.format"]` MUST be `text`
- AND the record MUST be exported to Tansu

---

## Log Format Catalog

### Format: JSON Structured

**Detection**: Body starts with `{` after trimming leading whitespace.

**Common sources**: Custom applications, some Kubernetes components (kube-proxy, etcd in structured logging mode), ArgoCD, Traefik.

**Example**:
```json
{"level":"info","ts":"2026-06-24T10:15:30.123Z","msg":"request completed","method":"GET","path":"/api/v1/users","status":200,"duration_ms":42}
```

**Target OTel mapping**:

| JSON Field | OTel Field | OTTL Expression |
|---|---|---|
| `level` / `severity` / `lvl` | `severity_text`, `severity_number` | `set(severity_text, body["level"])` + lookup table |
| `timestamp` / `time` / `ts` / `@timestamp` | `time_unix_nano` | `set(time, Time(body["ts"]))` |
| `msg` / `message` | `body` (if body is just the message) | Preserved in attributes if full JSON is body |
| `trace_id` / `traceId` | `trace_id` | `set(trace_id, body["trace_id"])` |
| `span_id` / `spanId` | `span_id` | `set(span_id, body["span_id"])` |
| All other fields | `attributes["<field_name>"]` | Flatten remaining keys into attributes |

**Severity mapping for JSON**:

| `level` value | severity_text | severity_number |
|---|---|---|
| `trace` | TRACE | 1 |
| `debug` | DEBUG | 5 |
| `info` | INFO | 9 |
| `warn` / `warning` | WARN | 13 |
| `error` / `err` | ERROR | 17 |
| `fatal` / `panic` / `critical` | FATAL | 21 |

---

### Format: glog

**Detection**: Body matches regex `^([IWEF])(\d{4} \d{2}:\d{2}:\d{2}\.\d+)\s+\d+\s+\S+:\d+]\s*(.*)$`

**Common sources**: Kubernetes control plane components (kube-apiserver, kube-scheduler, kube-controller-manager), Go services using `k8s.io/klog` or `golang/glog`.

**Example**:
```
I0624 10:15:30.123456   12345 server.go:42] Listening on :8080
W0624 10:15:31.234567   12345 reflector.go:125] watch of *v1.Pod ended with: too old resource version
E0624 10:15:32.345678   12345 controller.go:99] Failed to sync: connection refused
F0624 10:15:33.456789   12345 main.go:10] Unable to start: bind: address already in use
```

**Target OTel mapping**:

| glog Field | OTel Field | Extraction |
|---|---|---|
| Severity letter (I/W/E/F) | `severity_text`, `severity_number` | I→INFO/9, W→WARN/13, E→ERROR/17, F→FATAL/21 |
| Timestamp (`MMDD HH:MM:SS.micros`) | `time_unix_nano` | Parse with year inference from CRI prefix |
| File:line (`server.go:42`) | `attributes["log.source_file"]`, `attributes["log.source_line"]` | Regex capture |
| Thread ID (`12345`) | `attributes["log.thread_id"]` | Regex capture |
| Message (after `]`) | `body` | Everything after `] ` |

---

### Format: Key=Value

**Detection**: Body contains at least 3 `key=value` pairs (where value is not empty), separated by whitespace. Heuristic: `(\w+=[^\s]+)` matches ≥ 3 times.

**Common sources**: Go services using `log/slog` with key-value output, systemd journal exports, Envoy proxy, some CNCF projects.

**Example**:
```
ts=2026-06-24T10:15:30Z level=info msg="server started" port=8080 env=production
```

**Target OTel mapping**:

| KV Field | OTel Field | Notes |
|---|---|---|
| `level` / `lvl` | `severity_text`, `severity_number` | Same mapping as JSON |
| `ts` / `time` / `timestamp` | `time_unix_nano` | Parse to nanos |
| `msg` / `message` | `body` | Strip quotes if present |
| `err` / `error` | `attributes["error.message"]` | Preserve error detail |
| All other keys | `attributes["<key>"]` | Flatten into attributes |

---

### Format: Plain Text (Fallback)

**Detection**: Body does not match JSON, glog, or key=value heuristics.

**Common sources**: nginx access/error logs, raw application output, shell scripts, cron jobs.

**Example**:
```
10.0.0.5 - - [24/Jun/2026:10:15:30 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "kube-probe/1.31"
```

**Target OTel mapping**:

| Source | OTel Field | Notes |
|---|---|---|
| CRI timestamp | `time_unix_nano` | From CRI prefix |
| N/A (no detectable severity) | `severity_text=INFO`, `severity_number=9` | Default |
| Entire line | `body` | Preserved verbatim |
| `attributes["log.format"]` | `text` | Marker for downstream processing |

---

## Normalization Rules Summary

### Filelog Receiver Operators (First Pass)

The filelog receiver MUST apply operators in this order:

1. **`container` operator** — Parse CRI log format into `timestamp`, `attributes["log.iostream"]`, and `body`.
2. **`move` operator** — Move `attributes.log` to `body` (if needed after container parser).
3. **`json_parser` operator** — Conditional: if body starts with `{`, parse JSON into `attributes`.
4. **`regex_parser` operator** — Conditional: if body matches glog pattern, extract severity, timestamp, file, line.
5. **`key_value_parser` operator** — Conditional: if body contains ≥ 3 key=value pairs, parse into `attributes`.

### Transform Processor OTTL Statements (Second Stage)

The `transform` processor MUST apply these OTTL statement groups:

**Group 1: Format detection and tagging**
- If `body` starts with `{` → set `attributes["log.format"] = "json"`
- If `body` matches glog regex → set `attributes["log.format"] = "glog"`
- If `body` has ≥ 3 kv pairs → set `attributes["log.format"] = "keyvalue"`
- Default → set `attributes["log.format"] = "text"`

**Group 2: Severity normalization**
- Read severity from `attributes["level"]`, `attributes["severity"]`, `attributes["lvl"]` (JSON/kv)
- Read severity from first character of body (glog: I/W/E/F)
- Map to `severity_text` and `severity_number` using the canonical table
- Default to INFO/9 if no severity found

**Group 3: Timestamp normalization**
- For JSON: parse `attributes["timestamp"]`, `attributes["time"]`, `attributes["ts"]`, `attributes["@timestamp"]` to `time_unix_nano`
- For glog: parse `MMDD HH:MM:SS.micros` to `time_unix_nano` (year from CRI timestamp)
- For kv: parse `attributes["ts"]`, `attributes["time"]` to `time_unix_nano`
- Fallback: keep CRI-parsed `time_unix_nano`

**Group 4: Body cleanup**
- For JSON: optionally move `attributes["msg"]` or `attributes["message"]` to `body` if body was the raw JSON string
- For glog: set body to message portion (after `] `)
- For kv: set body to `attributes["msg"]` if present

**Group 5: Resource enrichment**
- Set `resource["cluster.name"]` = `homelab`

### k8sattributes Processor

The existing `k8sattributes` processor configuration MUST be reused for the logs pipeline with the same metadata extraction list:
- `k8s.namespace.name`
- `k8s.pod.name`
- `k8s.container.name`
- `k8s.node.name`
- `k8s.deployment.name`

### Batch Processor

The batch processor for the logs pipeline MUST use:
- `timeout: 5s` (lower than metrics to reduce log delivery latency)
- `send_batch_size: 512`
- `send_batch_max_size: 1024`

---

## Pipeline Stage Summary

```
filelog receiver
  ├── container operator (CRI parse)
  ├── move operator (body extraction)
  ├── json_parser (conditional)
  ├── regex_parser (glog, conditional)
  └── key_value_parser (conditional)
         │
         ▼
k8sattributes processor (enrich with k8s metadata)
         │
         ▼
transform processor (OTTL: format detection, severity normalization, timestamp extraction)
         │
         ▼
resource/cluster processor (cluster.name = homelab)
         │
         ▼
batch processor (5s timeout, 512 batch size)
         │
         ▼
memory_limiter processor (500 MiB limit)
         │
         ▼
otlp/tansu exporter (gRPC to tansu:4317)
         │
         ▼
Tansu Broker
  ├── Iceberg writer → S3 (otel_logs table)
  └── Kafka-compatible topics → Arroyo, Trino consumers
```

---

## Tansu Configuration Requirements

### Storage Backend

Tansu MUST use PostgreSQL as its storage backend for production durability. A dedicated PostgreSQL instance (or shared cluster PostgreSQL if available) MUST be provisioned. If no PostgreSQL is available at deployment time, SQLite MAY be used as a fallback with a documented migration path to PostgreSQL.

### Iceberg Catalog

Tansu MUST be configured with an Iceberg REST catalog or Hadoop catalog. The catalog MUST point to the Scaleway S3 bucket where Iceberg metadata and data files are written.

**S3 warehouse path**: `s3://logs/iceberg/` (under the existing `logs` bucket on Scaleway).

**Catalog configuration**:
- Iceberg namespace: `logs`
- Table name: `otel_logs`
- Partitioning: `k8s_namespace` (identity), `timestamp` (day transform)

### OTLP Ingestion

Tansu MUST expose an OTLP gRPC endpoint for direct log ingestion from the OTel Collector. The endpoint MUST be a Kubernetes Service of type ClusterIP on port 4317.

### Kafka Protocol

Tansu MUST expose a Kafka-compatible endpoint (port 9092) for downstream consumers (Arroyo, Trino Iceberg connector). Topics MUST map to the `otel_logs` Iceberg table.

---

## Query Layer Requirements

### Mandatory Queries (must work after deployment)

**Q1: All ERROR logs in the last hour for a namespace**
```sql
SELECT timestamp, k8s_pod, k8s_container, body
FROM otel_logs
WHERE k8s_namespace = '{namespace}'
  AND severity_text = 'ERROR'
  AND timestamp > now() - INTERVAL '1' HOUR
ORDER BY timestamp DESC;
```

**Q2: Find stack traces matching a pattern**
```sql
SELECT timestamp, k8s_pod, body
FROM otel_logs
WHERE body LIKE '%NullPointerException%'
  AND timestamp > now() - INTERVAL '24' HOUR
ORDER BY timestamp DESC;
```

**Q3: Log volume per namespace per hour**
```sql
SELECT k8s_namespace, date_trunc('hour', timestamp) AS hour, count(*) AS log_count
FROM otel_logs
WHERE timestamp > now() - INTERVAL '24' HOUR
GROUP BY k8s_namespace, hour
ORDER BY hour DESC;
```

**Q4: Error rate by service**
```sql
SELECT service_name, count(*) FILTER (WHERE severity_number >= 17) AS errors, count(*) AS total,
       round(count(*) FILTER (WHERE severity_number >= 17) * 100.0 / count(*), 2) AS error_pct
FROM otel_logs
WHERE timestamp > now() - INTERVAL '1' HOUR
GROUP BY service_name
ORDER BY error_pct DESC;
```

**Q5: Top log-producing pods**
```sql
SELECT k8s_namespace, k8s_pod, count(*) AS log_count
FROM otel_logs
WHERE timestamp > now() - INTERVAL '1' HOUR
GROUP BY k8s_namespace, k8s_pod
ORDER BY log_count DESC
LIMIT 20;
```

---

## Non-Functional Requirements

### Requirement: Latency Budget

The time from a log line being written to a container's stdout to its availability in the Iceberg table MUST be ≤ 60 seconds under normal load (≤ 1000 log lines/second cluster-wide).

#### Scenario: End-to-end latency

- GIVEN a pod writes a log line to stdout at time T
- WHEN the log line traverses the full pipeline (filelog → transform → batch → OTLP → Tansu → Iceberg)
- THEN the log line MUST be queryable in Iceberg by T + 60 seconds

---

### Requirement: Availability Impact

The logs pipeline MUST NOT affect the availability of the existing metrics pipeline. A failure in the logs pipeline (Tansu down, OTLP export errors) MUST NOT cause the metrics pipeline to degrade or lose data.

#### Scenario: Tansu failure isolation

- GIVEN the Tansu broker is unreachable
- WHEN the OTel Collector's OTLP/tansu exporter fails to send logs
- THEN the metrics pipeline (hostmetrics, kubeletstats → OTel Gateway → Prometheus) MUST continue operating normally
- AND the collector's `memory_limiter` MUST prevent log buffering from consuming metrics pipeline memory

---

### Requirement: Upgrade Strategy

The OTel Collector image version MUST be pinned (currently `otel/opentelemetry-collector-contrib:0.123.0`). Upgrades MUST be tested in a canary fashion: one node at a time, verifying log collection continuity.

#### Scenario: Rolling upgrade of collector

- GIVEN the OTel Collector DaemonSet image is updated
- WHEN ArgoCD performs a rolling update
- THEN at least N-1 nodes MUST remain collecting logs at all times (where N is the total node count)
- AND the updated pods MUST pass a readiness check confirming the logs pipeline is active

---

### Requirement: Historical Data Coexistence

Historical Parquet data in S3 (`s3://logs/raw/containers/`) MUST be left untouched. The Iceberg tables (`s3://logs/iceberg/`) are a separate dataset. Queries MAY span both datasets using Trino's multi-catalog support or DuckDB's ability to query multiple sources, but the system MUST NOT attempt to migrate Parquet to Iceberg.

#### Scenario: Parquet data unmodified

- GIVEN the log-normalization change is fully deployed
- WHEN inspecting the `s3://logs/raw/containers/` prefix
- THEN existing Parquet files MUST be unchanged
- AND new Parquet files MUST still be written by the FluentBit pipeline (parallel operation)

---

## Acceptance Criteria

| # | Criterion | Measurable Condition | Verification |
|---|---|---|---|
| AC-1 | Logs collected from all namespaces | Log records exist in Iceberg for every namespace with running pods | Query `SELECT DISTINCT k8s_namespace FROM otel_logs` and compare with `kubectl get namespaces` |
| AC-2 | JSON format parsed | JSON log fields appear as attributes | Ingest a known JSON log, query attributes in Iceberg |
| AC-3 | glog severity extracted | glog I/W/E/F mapped to correct severity_number | Ingest glog lines, verify severity_number in query results |
| AC-4 | Key=Value format parsed | KV pairs appear as attributes | Ingest a KV log, verify extracted attributes |
| AC-5 | Plain text preserved | Body of text logs is unchanged | Ingest a text log, compare body in Iceberg with original |
| AC-6 | Multiline reassembled | Java/Python/Go stack traces are single log records | Ingest a multiline trace, verify single record with full body |
| AC-7 | Severity default | Logs without severity have severity_number=9 | Ingest a log with no severity, verify default |
| AC-8 | K8s metadata populated | k8s_namespace, k8s_pod, k8s_container, k8s_node on all records | Query any log, verify all k8s fields non-null |
| AC-9 | Iceberg table queryable | Queries Q1-Q5 return results | Execute Q1-Q5 against Trino and DuckDB |
| AC-10 | Metrics pipeline unaffected | Prometheus metrics continue flowing | Check Prometheus targets and recent data points |
| AC-11 | Latency ≤ 60s | Log written at T is queryable by T+60s | Write timestamped log, query immediately |
| AC-12 | Collector memory < 600Mi | No OOM under normal load | Monitor `container_memory_working_set_bytes` for 24h |
| AC-13 | ArgoCD all green | All new Applications Synced + Healthy | `argocd app list` shows Synced + Healthy |
| AC-14 | FluentBit unchanged | FluentBit DaemonSet and Aggregator configs identical to pre-change | `kubectl get configmap -n monitoring fluentbit-daemonset-config -o yaml` diff |
| AC-15 | Parallel log counts within ±5% | Iceberg log count ≈ FluentBit Parquet log count over 24h | Compare row counts from both storage backends |
| AC-16 | Unknown formats not dropped | All logs reach Iceberg regardless of format | Ingest deliberately malformed log, verify it exists in Iceberg |
| AC-17 | Tansu recovery | Tansu survives restart without data loss | Kill Tansu pod, verify no gaps in Iceberg after recovery |

---

## Risks

| # | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R1 | **Multiline regex mismatch** | Stack traces split into multiple records, losing context | High | Test with real Java/Python/Go traces from the cluster. Use `flush_after` timeout as safety net. Arroyo second-pass can reassemble. |
| R2 | **OTel Collector CPU spike from transform** | Node resource pressure | Medium | Keep transform OTTL simple (no regex-heavy operators). Benchmark with production log volume. Use batch processor to amortize. |
| R3 | **Tansu Iceberg write stability** | Data loss or corruption if Tansu's Iceberg writer has bugs | Medium | Start with frequent small commits. Monitor Iceberg metadata for corruption. Keep FluentBit Parquet as safety net. |
| R4 | **Iceberg catalog incompatibility with Trino/DuckDB** | Query layer cannot read tables | Medium | Validate catalog type against Trino Iceberg connector docs and DuckDB iceberg extension before committing to catalog choice. |
| R5 | **filelog receiver offset tracking on restart** | Duplicate or missed log lines after collector restart | Low | filelog receiver uses persistent file-based offset tracking (not in-memory). Verify offset file is on a hostPath volume. |
| R6 | **S3 credential rotation** | Tansu loses S3 access if credentials change | Low | Use Kubernetes Secret for S3 credentials. Document rotation procedure. |
| R7 | **Increased node I/O from dual pipeline** | Both FluentBit and OTel reading same files doubles read I/O | Low | OS page cache serves both readers. Sequential tail reads are cheap. Monitor iowait. |
| R8 | **OTel Operator CRD version mismatch** | Collector CR rejected if CRD version is incompatible | Low | Pin collector image version. Test CR changes against current Operator version. |
