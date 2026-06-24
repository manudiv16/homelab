# Log Normalization — Technical Design

**Change ID**: `log-normalization`
**Status**: Design
**Date**: 2026-06-24
**Owner**: manudiv16
**Depends on**: proposal.md, spec.md

---

## 1. OTel Collector Configuration — Extending Existing CR

The existing `OpenTelemetryCollector` CR at `infrastructure/monitoring/otel-collector/collector.yaml` is extended with a `logs` pipeline. No new DaemonSet is deployed.

### 1.1 Updated collector.yaml (complete)

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: collector-daemonset
  namespace: monitoring
spec:
  mode: daemonset
  serviceAccount: otel-collector-daemonset
  image: otel/opentelemetry-collector-contrib:0.123.0
  resources:
    requests:
      cpu: 150m
      memory: 200Mi
    limits:
      cpu: 500m
      memory: 600Mi

  # HostPath volumes for filelog receiver offset tracking
  # and container log access
  volumes:
    - name: varlog
      hostPath:
        path: /var/log
    - name: varlibdockercontainers
      hostPath:
        path: /var/lib/docker/containers
    - name: otel-registry
      hostPath:
        path: /var/lib/otel/registry
        type: DirectoryOrCreate
  volumeMounts:
    - name: varlog
      mountPath: /var/log
      readOnly: true
    - name: varlibdockercontainers
      mountPath: /var/lib/docker/containers
      readOnly: true
    - name: otel-registry
      mountPath: /var/lib/otel

  config:
    receivers:
      # ── Existing receivers (unchanged) ──
      hostmetrics:
        collection_interval: 30s
        scrapers:
          cpu: {}
          memory: {}
          disk: {}
          network: {}
      kubeletstats:
        collection_interval: 30s
        auth_type: serviceAccount
        endpoint: ${env:K8S_NODE_NAME}:10250
        insecure_skip_verify: true

      # ── New: filelog receiver for container logs ──
      filelog:
        include:
          - /var/log/containers/*.log
        exclude:
          # Exclude fluentbit's own logs to avoid feedback loops
          - /var/log/containers/fluentbit-*.log
        # Offset tracking survives collector restarts
        storage: file_storage
        multiline:
          # Combine continuation lines into the preceding log entry.
          # This is a broad pattern — covers Java, Go, Python, Node.js.
          # Specific per-format parsing happens in the transform processor.
          line_start_pattern: '^(\d{4}-\d{2}-\d{2}|[IWEF]\d{4}|Traceback|panic|goroutine \d+)'
          flush_after: 5s
        operators:
          # Step 1: Parse CRI log format
          # Input:  "2026-06-24T10:15:30.123456789Z stdout F {\"level\":\"info\"}"
          # Output: body = "{\"level\":\"info\"}"
          #         attributes["log.iostream"] = "stdout"
          #         time = 2026-06-24T10:15:30.123456789Z
          - type: container
            id: cri-parser
            format: containerd

          # Step 2: Try JSON parsing (conditional)
          # Only fires if body starts with "{"
          - type: json_parser
            id: json-parser
            if: 'body startsWith "{"'
            parse_from: body
            parse_to: attributes
            # Preserve all JSON keys as attributes
            # The transform processor will extract severity, timestamp, etc.
            timestamp:
              parse_from: attributes.timestamp
              layout_type: gotime
              layout: "2006-01-02T15:04:05.999999999Z"
            severity:
              parse_from: attributes.level
            on_error: send_quiet

          # Step 3: Try glog regex parsing (conditional)
          # Matches: I0624 10:15:30.123456   12345 server.go:42] message
          - type: regex_parser
            id: glog-parser
            if: 'body matches "^([IWEF])\\d{4} \\d{2}:\\d{2}:\\d{2}\\."'
            regex: '^(?P<severity_letter>[IWEF])(?P<timestamp>\d{4} \d{2}:\d{2}:\d{2}\.\d+)\s+(?P<thread_id>\d+)\s+(?P<source_file>[^:]+):(?P<source_line>\d+)\]\s*(?P<message>.*)$'
            parse_from: body
            parse_to: attributes
            severity:
              parse_from: attributes.severity_letter
              mapping:
                I: info
                W: warn
                E: error
                F: fatal
            timestamp:
              parse_from: attributes.timestamp
              layout: "0102 15:04:05.000000"
              layout_type: gotime
            on_error: send_quiet

          # Step 4: Try key=value parsing (conditional)
          # Fires if body contains at least 3 key=value pairs
          - type: key_value_parser
            id: kv-parser
            if: 'body matches "\\w+=\\S+.*\\w+=\\S+.*\\w+=\\S+"'
            parse_from: body
            parse_to: attributes
            pair_delimiter: " "
            key_value_delimiter: "="
            on_error: send_quiet

    extensions:
      # Persistent file storage for filelog receiver offsets
      file_storage:
        directory: /var/lib/otel
        timeout: 1s
        compaction:
          on_start: true
          on_rebound: true
          max_transaction_size: 65536

    processors:
      # ── Existing processors (shared by metrics and logs) ──
      memory_limiter:
        check_interval: 5s
        limit_mib: 500
        spike_limit_mib: 150
      batch:
        timeout: 10s
      k8sattributes:
        auth_type: serviceAccount
        extract:
          metadata:
            - k8s.namespace.name
            - k8s.pod.name
            - k8s.container.name
            - k8s.node.name
            - k8s.deployment.name
      resource/cluster:
        attributes:
          - key: cluster.name
            value: homelab
            action: upsert

      # ── New: transform processor for log normalization ──
      transform/normalize-logs:
        log_statements:
          # ── Group 1: Format detection ──
          - context: log
            statements:
              # Default: tag as text until proven otherwise
              - set(attributes["log.format"], "text")

              # JSON detection (json_parser already parsed into attributes)
              - set(attributes["log.format"], "json")
                where IsString(body) and body startsWith "{"

              # glog detection (regex_parser already parsed severity_letter)
              - set(attributes["log.format"], "glog")
                where IsString(attributes["severity_letter"]) and attributes["severity_letter"] != ""

              # Key=Value detection (kv_parser puts kv keys in attributes)
              - set(attributes["log.format"], "keyvalue")
                where attributes["log.format"] == "text" and IsString(attributes["ts"])

          # ── Group 2: Severity normalization ──
          - context: log
            statements:
              # Default severity (for text logs with no detectable level)
              - set(severity_text, "INFO")
              - set(severity_number, 9)

              # JSON severity: check common field names
              - set(severity_text, "TRACE")
                where IsMatch(attributes["level"], "(?i)trace")
              - set(severity_number, 1)
                where IsMatch(attributes["level"], "(?i)trace")

              - set(severity_text, "DEBUG")
                where IsMatch(attributes["level"], "(?i)debug")
              - set(severity_number, 5)
                where IsMatch(attributes["level"], "(?i)debug")

              - set(severity_text, "INFO")
                where IsMatch(attributes["level"], "(?i)info")
              - set(severity_number, 9)
                where IsMatch(attributes["level"], "(?i)info")

              - set(severity_text, "WARN")
                where IsMatch(attributes["level"], "(?i)(warn|warning)")
              - set(severity_number, 13)
                where IsMatch(attributes["level"], "(?i)(warn|warning)")

              - set(severity_text, "ERROR")
                where IsMatch(attributes["level"], "(?i)(error|err)")
              - set(severity_number, 17)
                where IsMatch(attributes["level"], "(?i)(error|err)")

              - set(severity_text, "FATAL")
                where IsMatch(attributes["level"], "(?i)(fatal|panic|critical)")
              - set(severity_number, 21)
                where IsMatch(attributes["level"], "(?i)(fatal|panic|critical)")

              # Also check "severity" field (alternative JSON key)
              - set(severity_text, "TRACE")
                where IsMatch(attributes["severity"], "(?i)trace")
              - set(severity_number, 1)
                where IsMatch(attributes["severity"], "(?i)trace")

              - set(severity_text, "DEBUG")
                where IsMatch(attributes["severity"], "(?i)debug")
              - set(severity_number, 5)
                where IsMatch(attributes["severity"], "(?i)debug")

              - set(severity_text, "INFO")
                where IsMatch(attributes["severity"], "(?i)info")
              - set(severity_number, 9)
                where IsMatch(attributes["severity"], "(?i)info")

              - set(severity_text, "WARN")
                where IsMatch(attributes["severity"], "(?i)(warn|warning)")
              - set(severity_number, 13)
                where IsMatch(attributes["severity"], "(?i)(warn|warning)")

              - set(severity_text, "ERROR")
                where IsMatch(attributes["severity"], "(?i)(error|err)")
              - set(severity_number, 17)
                where IsMatch(attributes["severity"], "(?i)(error|err)")

              - set(severity_text, "FATAL")
                where IsMatch(attributes["severity"], "(?i)(fatal|panic|critical)")
              - set(severity_number, 21)
                where IsMatch(attributes["severity"], "(?i)(fatal|panic|critical)")

              # glog severity: already handled by regex_parser's severity mapping.
              # Fallback if regex_parser didn't set it:
              - set(severity_text, "INFO")
                where attributes["log.format"] == "glog" and severity_number < 9
              - set(severity_number, 9)
                where attributes["log.format"] == "glog" and severity_number < 9

          # ── Group 3: Timestamp normalization ──
          - context: log
            statements:
              # JSON timestamp: try common field names
              # (json_parser may have already extracted via its timestamp config)
              - set(time, Time(attributes["timestamp"], "%Y-%m-%dT%H:%M:%S.%LZ"))
                where attributes["log.format"] == "json" and IsString(attributes["timestamp"])
              - set(time, Time(attributes["ts"], "%Y-%m-%dT%H:%M:%SZ"))
                where attributes["log.format"] == "json" and IsString(attributes["ts"])
              - set(time, Time(attributes["@timestamp"], "%Y-%m-%dT%H:%M:%S.%LZ"))
                where attributes["log.format"] == "json" and IsString(attributes["@timestamp"])

              # KV timestamp
              - set(time, Time(attributes["ts"], "%Y-%m-%dT%H:%M:%SZ"))
                where attributes["log.format"] == "keyvalue" and IsString(attributes["ts"])
              - set(time, Time(attributes["time"], "%Y-%m-%dT%H:%M:%SZ"))
                where attributes["log.format"] == "keyvalue" and IsString(attributes["time"])

              # glog timestamp: already handled by regex_parser's timestamp config.
              # Falls back to CRI timestamp if regex_parser failed.

              # text: keep CRI timestamp (already set by container operator). No action.

          # ── Group 4: Body cleanup ──
          - context: log
            statements:
              # JSON: move "msg" or "message" to body for cleaner queries
              - set(body, attributes["msg"])
                where attributes["log.format"] == "json" and IsString(attributes["msg"])
              - set(body, attributes["message"])
                where attributes["log.format"] == "json" and IsString(attributes["message"]) and not IsString(attributes["msg"])

              # glog: message already extracted to attributes["message"] by regex
              - set(body, attributes["message"])
                where attributes["log.format"] == "glog" and IsString(attributes["message"])

              # KV: move "msg" to body
              - set(body, attributes["msg"])
                where attributes["log.format"] == "keyvalue" and IsString(attributes["msg"])
              - set(body, attributes["message"])
                where attributes["log.format"] == "keyvalue" and IsString(attributes["message"]) and not IsString(attributes["msg"])

          # ── Group 5: Attribute cleanup ──
          # Remove internal parser artifacts from attributes to keep the
          # attribute map clean for downstream consumers (Tansu/Iceberg).
          - context: log
            statements:
              - delete_key(attributes, "severity_letter")
              - delete_key(attributes, "source_file")
              - delete_key(attributes, "source_line")
              - delete_key(attributes, "thread_id")
              - delete_key(attributes, "log.iostream")
              # Keep: log.format, level, msg, message, ts, timestamp,
              #       and all user-defined JSON/KV fields

      # ── New: batch processor specifically for logs ──
      batch/logs:
        timeout: 5s
        send_batch_size: 512
        send_batch_max_size: 1024

    exporters:
      # ── Existing exporter (metrics → gateway, unchanged) ──
      otlp:
        endpoint: otel-gateway-collector:4317
        tls:
          insecure: true

      # ── New exporter: logs → Tansu ──
      otlp/tansu:
        endpoint: tansu-broker.monitoring.svc.cluster.local:4317
        tls:
          insecure: true
        retry_on_failure:
          enabled: true
          initial_interval: 5s
          max_interval: 30s
          max_elapsed_time: 300s
        sending_queue:
          enabled: true
          queue_size: 5000
          num_consumers: 4

    service:
      extensions:
        - file_storage
      pipelines:
        # Existing metrics pipeline (unchanged)
        metrics:
          receivers:
            - hostmetrics
            - kubeletstats
          processors:
            - memory_limiter
            - k8sattributes
            - resource/cluster
            - batch
          exporters:
            - otlp
        # New logs pipeline
        logs:
          receivers:
            - filelog
          processors:
            - memory_limiter
            - k8sattributes
            - transform/normalize-logs
            - resource/cluster
            - batch/logs
          exporters:
            - otlp/tansu
      telemetry:
        metrics:
          address: 0.0.0.0:8888
```

### 1.2 Key Configuration Decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Filelog receiver vs. FluentBit** | Use `filelog` receiver in same DaemonSet | Avoids additional DaemonSet; reuses existing k8sattributes config |
| **Multiline strategy** | Broad `line_start_pattern` in filelog + per-format handling in transform | Catches most multiline at ingestion; Arroyo handles edge cases in second pass |
| **Container operator** | `format: containerd` | k3s uses containerd as its CRI runtime |
| **Conditional parsing** | `if:` predicates on operators | Avoids wasting CPU parsing JSON as regex when it's already JSON |
| **Transform processor** | Separate from metrics `batch` processor | Different timeout/batch settings for latency-sensitive logs |
| **OTLP exporter** | Separate `otlp/tansu` exporter | Isolates logs from metrics export path — failures in one don't affect the other |
| **Extensions** | `file_storage` extension | Persists filelog receiver offsets to hostPath volume; survives pod restarts |
| **Memory limit** | `limit_mib: 500` (up from 400) | Accommodates logs pipeline buffering; stays within 600Mi container limit |

### 1.3 Operator Processing Chain Detail

The filelog receiver processes each CRI log line through this chain:

```
Raw line from /var/log/containers/pod_ns_container-id.log:
  "2026-06-24T10:15:30.123456789Z stdout F {"level":"info","msg":"started"}"

│
▼ container operator (cri-parser)
├── body:         '{"level":"info","msg":"started"}'
├── time:         2026-06-24T10:15:30.123456789Z
├── attributes:
│   └── log.iostream: "stdout"
│
▼ json_parser (json-parser, if: body startsWith "{")
├── body:         '{"level":"info","msg":"started"}'  (unchanged)
├── attributes:
│   ├── log.iostream: "stdout"
│   ├── level:        "info"
│   ├── msg:          "started"
│   └── (other JSON keys...)
├── severity_text:  "info"  (from severity parse)
├── severity_number: 9      (mapped by parser)
└── time:           (unchanged — json_parser timestamp parse may update)

▼ regex_parser (glog-parser, skipped — body doesn't match glog)
▼ key_value_parser (kv-parser, skipped — body doesn't match KV pattern)

▼ transform/normalize-logs processor (OTTL)
├── attributes["log.format"]: "json"  (detected)
├── severity_text:  "INFO"  (normalized to uppercase)
├── severity_number: 9
├── body:           "started"  (moved from attributes["msg"])
└── attributes:
    ├── log.format: "json"
    ├── level:      "info"
    └── (other JSON keys, iostream removed)

▼ k8sattributes processor
└── resource:
    ├── k8s.namespace.name:  "production"
    ├── k8s.pod.name:        "my-app-abc123"
    ├── k8s.container.name:  "web"
    ├── k8s.node.name:       "k3s-worker-1"
    └── k8s.deployment.name: "my-app"

▼ resource/cluster processor
└── resource:
    └── cluster.name: "homelab"

▼ batch/logs processor
└── (batched into groups of ≤1024, flushed every 5s)

▼ otlp/tansu exporter
└── gRPC → tansu-broker:4317
```

### 1.4 RBAC Changes

The existing `infrastructure/monitoring/otel-collector/rbac.yaml` requires no changes. The `filelog` receiver reads from the host filesystem (volumes), not the Kubernetes API. The existing `k8sattributes` permissions (`get`, `list`, `watch` on pods, namespaces, nodes, deployments) are sufficient for both pipelines.

### 1.5 Filelog Offset Storage

The `file_storage` extension writes checkpoint files to `/var/lib/otel` inside the container, backed by a `hostPath` volume at `/var/lib/otel/registry` on the node. This ensures:

- Offset tracking survives collector pod restarts (not lost on OOMKill)
- Each node's DaemonSet pod tracks its own offsets independently
- The `compaction` settings prevent checkpoint file growth over time

---

## 2. Tansu Broker Deployment

### 2.1 Namespace

Tansu deploys to the `monitoring` namespace (same as the collector and gateway). No new namespace is created.

### 2.2 Directory Structure

```
infrastructure/tansu/
├── statefulset.yaml     # Tansu StatefulSet
├── service.yaml         # ClusterIP Service (OTLP + Kafka ports)
├── rbac.yaml            # ServiceAccount + RBAC (if needed)
├── configmap.yaml       # Tansu configuration
├── secret.yaml          # S3 credentials + PostgreSQL DSN
└── pvc.yaml             # PVC for offset tracking (if not using postgres)
```

### 2.3 StatefulSet

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: tansu-broker
  namespace: monitoring
  labels:
    app.kubernetes.io/name: tansu
    app.kubernetes.io/component: broker
spec:
  serviceName: tansu-broker
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: tansu
  template:
    metadata:
      labels:
        app.kubernetes.io/name: tansu
    spec:
      serviceAccountName: tansu-broker
      containers:
        - name: tansu
          image: ghcr.io/rustyconover/tansu:latest  # Pin to specific tag in production
          args:
            - "--all-features"
          env:
            # ── Storage backend: PostgreSQL ──
            - name: TANSU_STORAGE_ENGINE
              value: "postgres"
            - name: TANSU_STORAGE_POSTGRES_DSN
              valueFrom:
                secretKeyRef:
                  name: tansu-secrets
                  key: postgres-dsn
            # ── Kafka-compatible broker ──
            - name: TANSU_KAFKA_LISTENER
              value: "PLAINTEXT://0.0.0.0:9092"
            - name: TANSU_KAFKA_ADVERTISED_LISTENER
              value: "PLAINTEXT://tansu-broker.monitoring.svc.cluster.local:9092"
            # ── OTLP ingestion ──
            - name: TANSU_OTLP_LISTENER
              value: "0.0.0.0:4317"
            # ── Iceberg configuration ──
            - name: TANSU_ICEBERG_CATALOG_TYPE
              value: "rest"
            - name: TANSU_ICEBERG_CATALOG_URI
              value: "http://localhost:8181"  # Local REST catalog or external
            - name: TANSU_ICEBERG_WAREHOUSE
              value: "s3://logs/iceberg"
            - name: TANSU_ICEBERG_NAMESPACE
              value: "logs"
            - name: TANSU_ICEBERG_S3_ENDPOINT
              value: "https://s3.fr-par.scw.cloud"
            - name: TANSU_ICEBERG_S3_REGION
              value: "fr-par"
            - name: TANSU_ICEBERG_S3_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: tansu-secrets
                  key: s3-access-key
            - name: TANSU_ICEBERG_S3_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: tansu-secrets
                  key: s3-secret-key
          ports:
            - name: otlp-grpc
              containerPort: 4317
              protocol: TCP
            - name: kafka
              containerPort: 9092
              protocol: TCP
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
          readinessProbe:
            tcpSocket:
              port: 9092
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            tcpSocket:
              port: 9092
            initialDelaySeconds: 30
            periodSeconds: 30
          volumeMounts:
            - name: tansu-data
              mountPath: /var/lib/tansu
  volumeClaimTemplates:
    - metadata:
        name: tansu-data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 5Gi
```

### 2.4 Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: tansu-broker
  namespace: monitoring
  labels:
    app.kubernetes.io/name: tansu
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: tansu
  ports:
    - name: otlp-grpc
      port: 4317
      targetPort: 4317
      protocol: TCP
    - name: kafka
      port: 9092
      targetPort: 9092
      protocol: TCP
```

### 2.5 Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: tansu-secrets
  namespace: monitoring
  labels:
    app.kubernetes.io/name: tansu
type: Opaque
stringData:
  # PostgreSQL DSN for Tansu's internal state
  # Option A: Dedicated PostgreSQL
  postgres-dsn: "postgresql://tansu:tansu@tansu-pg:5432/tansu?sslmode=disable"
  # Option B: SQLite fallback (set TANSU_STORAGE_ENGINE=sqlite, ignore DSN)
  # postgres-dsn: "sqlite:///var/lib/tansu/state.db"

  # Scaleway S3 credentials for Iceberg writes
  s3-access-key: "${SCALEWAY_S3_ACCESS_KEY}"   # Replace at deploy time
  s3-secret-key: "${SCALEWAY_S3_SECRET_KEY}"   # Replace at deploy time
```

### 2.6 PostgreSQL for Tansu (Optional Dedicated Instance)

If the homelab doesn't have a shared PostgreSQL instance, deploy a lightweight one:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: tansu-pg
  namespace: monitoring
  labels:
    app.kubernetes.io/name: tansu-pg
spec:
  serviceName: tansu-pg
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: tansu-pg
  template:
    metadata:
      labels:
        app.kubernetes.io/name: tansu-pg
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - name: POSTGRES_DB
              value: tansu
            - name: POSTGRES_USER
              value: tansu
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: tansu-secrets
                  key: postgres-password
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          ports:
            - containerPort: 5432
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 250m
              memory: 256Mi
          volumeMounts:
            - name: pg-data
              mountPath: /var/lib/postgresql/data
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "tansu"]
            initialDelaySeconds: 5
            periodSeconds: 10
  volumeClaimTemplates:
    - metadata:
        name: pg-data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 2Gi
---
apiVersion: v1
kind: Service
metadata:
  name: tansu-pg
  namespace: monitoring
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: tansu-pg
  ports:
    - port: 5432
      targetPort: 5432
```

### 2.7 Initial Tansu Setup Commands

After Tansu is running, create the topic that maps to the Iceberg table:

```bash
# Create the otel-logs topic via Kafka CLI (using Tansu's Kafka endpoint)
kubectl -n monitoring exec -it deploy/tansu-broker -- \
  tansu admin create-topic \
    --bootstrap-server localhost:9092 \
    --topic otel-logs \
    --partitions 3 \
    --replication-factor 1

# Create the Iceberg namespace (if Tansu doesn't auto-create)
kubectl -n monitoring exec -it deploy/tansu-broker -- \
  tansu admin create-iceberg-namespace \
    --catalog-uri http://localhost:8181 \
    --namespace logs

# Verify Iceberg table creation
kubectl -n monitoring exec -it deploy/tansu-broker -- \
  tansu admin describe-iceberg-table \
    --catalog-uri http://localhost:8181 \
    --namespace logs \
    --table otel_logs
```

> **Note**: The exact Tansu CLI subcommands depend on the version deployed. These commands are representative — adjust based on `tansu --help` output after initial deployment. If Tansu auto-creates topics and Iceberg tables from OTLP input, manual topic creation may not be needed.

---

## 3. Iceberg Table Schema

### 3.1 Column Definitions

The `otel_logs` Iceberg table maps directly to the OTel Log Data Model:

```sql
-- Iceberg DDL (executed via Trino or Tansu admin)
CREATE TABLE IF NOT EXISTS iceberg.logs.otel_logs (
    -- Core OTel fields
    timestamp            TIMESTAMP(9) WITH TIME ZONE  NOT NULL COMMENT 'Event time (OTel time_unix_nano)',
    observed_timestamp   TIMESTAMP(9) WITH TIME ZONE  NOT NULL COMMENT 'Collector observation time',
    severity_number      INTEGER                      NOT NULL COMMENT 'OTel severity: 1=TRACE..24=FATAL',
    severity_text        VARCHAR(16)                  NOT NULL COMMENT 'Human severity: TRACE/DEBUG/INFO/WARN/ERROR/FATAL',
    body                 VARCHAR                      NOT NULL COMMENT 'Log message body (after normalization)',

    -- Distributed tracing correlation
    trace_id             VARCHAR(32)                  COMMENT 'W3C trace context trace-id (hex)',
    span_id              VARCHAR(16)                  COMMENT 'W3C trace context span-id (hex)',

    -- Kubernetes resource attributes (top-level for partitioning + fast filtering)
    k8s_namespace        VARCHAR(253)                 NOT NULL COMMENT 'Pod namespace',
    k8s_pod              VARCHAR(253)                 NOT NULL COMMENT 'Pod name',
    k8s_container        VARCHAR(253)                 NOT NULL COMMENT 'Container name',
    k8s_node             VARCHAR(253)                 NOT NULL COMMENT 'Node name',
    k8s_deployment       VARCHAR(253)                 COMMENT 'Deployment name (if applicable)',
    service_name         VARCHAR(253)                 COMMENT 'Service name (from app.kubernetes.io/name label)',

    -- Cluster metadata
    cluster_name         VARCHAR(63)                  NOT NULL COMMENT 'Cluster identifier',

    -- Log format metadata
    log_format           VARCHAR(16)                  NOT NULL COMMENT 'Detected format: json/glog/keyvalue/text',

    -- Catch-all for remaining attributes
    attributes           MAP(VARCHAR, VARCHAR)         COMMENT 'Remaining key-value attributes from the log record'
)
WITH (
    format = 'PARQUET',
    partitioning = ARRAY['k8s_namespace', 'day(timestamp)'],
    sorted_by = ARRAY['timestamp'],
    write.format.default = 'parquet',
    write.parquet.compression-codec = 'zstd'
);
```

### 3.2 Partitioning Strategy

| Partition Column | Transform | Granularity | Rationale |
|---|---|---|---|
| `k8s_namespace` | identity | per-namespace | Enables fast namespace-scoped queries (most queries filter by namespace) |
| `timestamp` | `day(timestamp)` | per-day | Enables time-range pruning; day granularity balances file count vs. pruning |

**Expected partition layout on S3:**
```
s3://logs/iceberg/logs/otel_logs/data/
├── k8s_namespace=monitoring/
│   ├── timestamp_day=2026-06-24/
│   │   ├── 00000-0-abc123.parquet
│   │   └── 00001-0-def456.parquet
│   └── timestamp_day=2026-06-25/
│       └── ...
├── k8s_namespace=production/
│   ├── timestamp_day=2026-06-24/
│   └── ...
└── k8s_namespace=kube-system/
    └── ...
```

### 3.3 Sorting / Clustering

```sql
-- Rows within each partition are sorted by timestamp
-- This improves scan performance for time-range queries
sorted_by = ARRAY['timestamp']
```

Sorting by `timestamp` ensures that within a partition, log files are physically ordered by time. This is especially beneficial for:
- Time-range scans (most common query pattern)
- Point-in-time debugging (find logs around a specific event)
- Compaction (adjacent time ranges are co-located)

### 3.4 Schema Evolution Approach

Iceberg supports additive schema changes without rewriting data:

```sql
-- Future: promote a frequently-queried attribute to a top-level column
ALTER TABLE iceberg.logs.otel_logs ADD COLUMN log_correlation_id VARCHAR(128);

-- Future: promote error.message for better query ergonomics
ALTER TABLE iceberg.logs.otel_logs ADD COLUMN error_message VARCHAR;
```

**Rules:**
- New attributes from JSON/KV logs go into the `attributes` map column
- Promote to top-level columns only for attributes queried by ≥ 3 queries
- Iceberg column IDs are stable — existing data files return NULL for new columns
- No data migration needed — old files are read with the schema at write time

### 3.5 Map Column Implementation

The `attributes` column stores all remaining key-value pairs that were not promoted to top-level columns. This includes:

- JSON fields not mapped to OTel core fields (e.g., `method`, `path`, `status`, `duration_ms`)
- KV pairs not mapped to core fields (e.g., `env`, `port`)
- glog metadata (e.g., `source_file`, `source_line` if not cleaned up)

**Note**: Iceberg's `MAP` column type stores nested data in Parquet as a repeated group. Trino and DuckDB both support querying map columns:

```sql
-- Trino: access map values
SELECT attributes['method'] AS http_method, attributes['path'] AS endpoint
FROM iceberg.logs.otel_logs
WHERE k8s_namespace = 'production' AND log_format = 'json';

-- DuckDB: similar syntax
SELECT attributes['method'] AS http_method, attributes['path'] AS endpoint
FROM iceberg_scan('s3://logs/iceberg/logs/otel_logs')
WHERE k8s_namespace = 'production' AND log_format = 'json';
```

---

## 4. Query Layer

### 4.1 Trino

#### Deployment

```
infrastructure/iceberg-query/
├── trino/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── configmap.yaml         # trino catalog config, etc.
│   └── coordinator-config.yaml # node.properties, jvm.config
```

**Trino Deployment:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: trino
  namespace: monitoring
  labels:
    app.kubernetes.io/name: trino
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: trino
  template:
    metadata:
      labels:
        app.kubernetes.io/name: trino
    spec:
      containers:
        - name: trino
          image: trinodb/trino:468
          ports:
            - containerPort: 8080
              name: http
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              cpu: "1"
              memory: 1Gi
          volumeMounts:
            - name: config
              mountPath: /etc/trino/catalog
          readinessProbe:
            httpGet:
              path: /v1/info
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /v1/info
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 30
      volumes:
        - name: config
          configMap:
            name: trino-catalog-config
```

**Trino Service:**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: trino
  namespace: monitoring
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: trino
  ports:
    - port: 8080
      targetPort: 8080
      name: http
```

**Trino Iceberg Catalog Config (ConfigMap):**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: trino-catalog-config
  namespace: monitoring
data:
  iceberg.properties: |
    connector.name=iceberg
    iceberg.catalog.type=rest
    iceberg.rest.uri=http://tansu-broker.monitoring.svc.cluster.local:8181
    # Or direct S3 + Hadoop catalog if REST catalog is not available:
    # connector.name=iceberg
    # iceberg.catalog.type=hadoop
    # iceberg.warehouse=s3://logs/iceberg
    s3.endpoint=https://s3.fr-par.scw.cloud
    s3.region=fr-par
    s3.access-key=${env:AWS_ACCESS_KEY_ID}
    s3.secret-key=${env:AWS_SECRET_ACCESS_KEY}
    s3.path-style-access=true
    iceberg.table-statistics-enabled=false
```

#### Example Trino Queries

```sql
-- Q1: All ERROR logs in the last hour for a namespace
SELECT timestamp, k8s_pod, k8s_container, body
FROM iceberg.logs.otel_logs
WHERE k8s_namespace = 'production'
  AND severity_text = 'ERROR'
  AND timestamp > now() - INTERVAL '1' HOUR
ORDER BY timestamp DESC;

-- Q2: Find stack traces matching a pattern
SELECT timestamp, k8s_pod, body
FROM iceberg.logs.otel_logs
WHERE body LIKE '%NullPointerException%'
  AND timestamp > now() - INTERVAL '24' HOUR
ORDER BY timestamp DESC;

-- Q3: Log volume per namespace per hour
SELECT
  k8s_namespace,
  date_trunc('hour', timestamp) AS hour,
  count(*) AS log_count
FROM iceberg.logs.otel_logs
WHERE timestamp > now() - INTERVAL '24' HOUR
GROUP BY k8s_namespace, hour
ORDER BY hour DESC;

-- Q4: Error rate by service
SELECT
  service_name,
  count(*) FILTER (WHERE severity_number >= 17) AS errors,
  count(*) AS total,
  round(
    count(*) FILTER (WHERE severity_number >= 17) * 100.0 / count(*),
    2
  ) AS error_pct
FROM iceberg.logs.otel_logs
WHERE timestamp > now() - INTERVAL '1' HOUR
GROUP BY service_name
ORDER BY error_pct DESC;

-- Q5: Top log-producing pods
SELECT k8s_namespace, k8s_pod, count(*) AS log_count
FROM iceberg.logs.otel_logs
WHERE timestamp > now() - INTERVAL '1' HOUR
GROUP BY k8s_namespace, k8s_pod
ORDER BY log_count DESC
LIMIT 20;

-- Q6: JSON attributes exploration
-- Find which attributes are available for a given format
SELECT
  map_keys(attributes) AS attr_keys,
  count(*) AS cnt
FROM iceberg.logs.otel_logs
WHERE log_format = 'json'
  AND timestamp > now() - INTERVAL '1' HOUR
GROUP BY map_keys(attributes)
ORDER BY cnt DESC
LIMIT 20;

-- Q7: Query specific JSON attributes
SELECT
  timestamp,
  k8s_pod,
  attributes['method'] AS http_method,
  attributes['path'] AS endpoint,
  attributes['status'] AS status_code,
  body
FROM iceberg.logs.otel_logs
WHERE log_format = 'json'
  AND k8s_namespace = 'production'
  AND timestamp > now() - INTERVAL '1' HOUR
ORDER BY timestamp DESC;
```

### 4.2 DuckDB — Ad-Hoc Queries

DuckDB is used for local exploration and validation. It can read Iceberg tables directly from S3.

**Usage: via `kubectl exec` into a Trino pod (or a dedicated debug pod) or locally with DuckDB CLI.**

#### .duckdbrc (or init.sql)

```sql
-- Load required extensions
INSTALL iceberg;
LOAD iceberg;
INSTALL httpfs;
LOAD httpfs;

-- Configure S3 credentials for Scaleway
SET s3_endpoint = 's3.fr-par.scw.cloud';
SET s3_region = 'fr-par';
SET s3_access_key_id = '${SCALEWAY_S3_ACCESS_KEY}';
SET s3_secret_access_key = '${SCALEWAY_S3_SECRET_KEY}';
SET s3_url_style = 'path';

-- Create a convenience view
CREATE OR REPLACE VIEW otel_logs AS
SELECT * FROM iceberg_scan('s3://logs/iceberg/logs/otel_logs/metadata');
```

#### Example DuckDB Commands

```bash
# Install and configure DuckDB locally (if not already installed)
brew install duckdb

# Query Iceberg table from local DuckDB
duckdb -c "
  INSTALL iceberg;
  LOAD iceberg;
  INSTALL httpfs;
  LOAD httpfs;
  SET s3_endpoint = 's3.fr-par.scw.cloud';
  SET s3_region = 'fr-par';
  SET s3_access_key_id = getenv('SCALEWAY_S3_ACCESS_KEY');
  SET s3_secret_access_key = getenv('SCALEWAY_S3_SECRET_KEY');
  SET s3_url_style = 'path';
  SELECT severity_text, count(*)
  FROM iceberg_scan('s3://logs/iceberg/logs/otel_logs/metadata')
  GROUP BY severity_text
  ORDER BY count(*) DESC;
"

# Validate log counts: compare Iceberg vs Parquet (FluentBit output)
duckdb -c "
  INSTALL iceberg;
  LOAD iceberg;
  INSTALL httpfs;
  LOAD httpfs;
  SET s3_endpoint = 's3.fr-par.scw.cloud';
  SET s3_region = 'fr-par';
  SET s3_access_key_id = getenv('SCALEWAY_S3_ACCESS_KEY');
  SET s3_secret_access_key = getenv('SCALEWAY_S3_SECRET_KEY');
  SET s3_url_style = 'path';
  SELECT 'iceberg' AS source, count(*) AS log_count
  FROM iceberg_scan('s3://logs/iceberg/logs/otel_logs/metadata')
  WHERE timestamp > now() - INTERVAL '24' HOUR
  UNION ALL
  SELECT 'parquet' AS source, count(*) AS log_count
  FROM read_parquet('s3://logs/raw/containers/year=2026/month=06/day=24/**/*.parquet');
"
```

### 4.3 Arroyo — Streaming SQL

**Deployment:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: arroyo
  namespace: monitoring
  labels:
    app.kubernetes.io/name: arroyo
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: arroyo
  template:
    metadata:
      labels:
        app.kubernetes.io/name: arroyo
    spec:
      containers:
        - name: arroyo
          image: ghcr.io/arroyosystems/arroyo:latest  # Pin to specific tag
          ports:
            - containerPort: 8000
              name: api
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
          env:
            # Arroyo connects to Tansu as a Kafka-compatible source
            - name: ARROYO__KAFKA_BOOTSTRAP_SERVERS
              value: "tansu-broker.monitoring.svc.cluster.local:9092"
            - name: ARROYO__ICEBERG_CATALOG_URI
              value: "http://tansu-broker.monitoring.svc.cluster.local:8181"
            - name: ARROYO__ICEBERG_WAREHOUSE
              value: "s3://logs/iceberg"
          readinessProbe:
            httpGet:
              path: /api/v1/status
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 10
```

**Arroyo Service:**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: arroyo
  namespace: monitoring
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: arroyo
  ports:
    - port: 8000
      targetPort: 8000
      name: api
```

#### Example Arroyo Streaming SQL Pipeline

```sql
-- Second-pass normalization: detect multiline stack traces that
-- the first pass may have split, and reassemble them.
-- Also applies cross-log correlation heuristics.

CREATE TABLE otel_logs_source (
    timestamp TIMESTAMP(9),
    observed_timestamp TIMESTAMP(9),
    severity_number INT,
    severity_text VARCHAR,
    body VARCHAR,
    trace_id VARCHAR,
    span_id VARCHAR,
    k8s_namespace VARCHAR,
    k8s_pod VARCHAR,
    k8s_container VARCHAR,
    k8s_node VARCHAR,
    k8s_deployment VARCHAR,
    service_name VARCHAR,
    cluster_name VARCHAR,
    log_format VARCHAR,
    attributes MAP<VARCHAR, VARCHAR>
) WITH (
    connector = 'kafka',
    bootstrap_servers = 'tansu-broker:9092',
    topic = 'otel-logs',
    format = 'json',
    'scan.startup.mode' = 'latest-offset'
);

-- Reassemble split stack traces within a 10-second window
CREATE TABLE normalized_logs AS
SELECT
    window_start AS timestamp,
    k8s_namespace,
    k8s_pod,
    k8s_container,
    k8s_node,
    severity_number,
    -- If any line in the window is ERROR or above, use that severity
    CASE
        WHEN max(severity_number) >= 17 THEN 'ERROR'
        WHEN max(severity_number) >= 13 THEN 'WARN'
        ELSE 'INFO'
    END AS severity_text,
    -- Concatenate body lines (for multiline reassembly)
    list_agg(body, '\n') WITHIN GROUP (ORDER BY timestamp) AS body,
    log_format,
    -- Mark records that were reassembled
    CASE
        WHEN count(*) > 1 THEN 'reassembled'
        ELSE 'original'
    END AS processing_state
FROM otel_logs_source
GROUP BY
    k8s_namespace,
    k8s_pod,
    k8s_container,
    k8s_node,
    service_name,
    log_format,
    -- Tumbling window: group logs within 10-second windows
    HOP(INTERVAL '10' SECOND, INTERVAL '10' SECOND);

-- Write normalized results to a derived Iceberg table
CREATE TABLE normalized_logs_sink
WITH (
    connector = 'iceberg',
    catalog_uri = 'http://tansu-broker:8181',
    warehouse = 's3://logs/iceberg',
    namespace = 'logs',
    table_name = 'otel_logs_normalized'
) AS SELECT * FROM normalized_logs;
```

---

## 5. ArgoCD Integration

### 5.1 New ArgoCD Application Definitions

**`apps/tansu.yaml`:**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: tansu
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "3"
spec:
  project: default
  source:
    repoURL: https://github.com/manudiv16/homelab.git
    targetRevision: main
    path: infrastructure/tansu
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

**`apps/iceberg-query.yaml`:**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: iceberg-query
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "4"
spec:
  project: default
  source:
    repoURL: https://github.com/manudiv16/homelab.git
    targetRevision: main
    path: infrastructure/iceberg-query
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

**`apps/arroyo.yaml`:**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: arroyo
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "4"
spec:
  project: default
  source:
    repoURL: https://github.com/manudiv16/homelab.git
    targetRevision: main
    path: infrastructure/arroyo
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

### 5.2 Existing Application Modifications

**`apps/otel-collector.yaml`**: No changes required. The existing Application already points to `infrastructure/monitoring/otel-collector/` with `recurse: true`. Any changes to `collector.yaml` in that directory are automatically picked up by ArgoCD.

### 5.3 Sync Wave Summary

| Wave | Application | Component | Rationale |
|---|---|---|---|
| 1 | `otel-operator` (existing) | OTel Operator CRDs | Must be present before any OpenTelemetryCollector CR can be applied |
| 2 | `otel-collector` (existing, modified) | OTel Collector DaemonSet | Depends on operator (wave 1). New logs pipeline needs Tansu Service (wave 3), but the collector's OTLP exporter has retry logic that handles Tansu not being ready yet |
| 3 | `tansu` (new) | Tansu Broker | Depends on monitoring namespace existing. Must be up before Arroyo/Trino (wave 4) |
| 3 | `fluentbit` (existing) | FluentBit DaemonSet + Aggregator | Unchanged |
| 3 | `otel-gateway` (existing) | OTel Gateway | Unchanged |
| 4 | `iceberg-query` (new) | Trino | Depends on Tansu (wave 3) for Iceberg catalog |
| 4 | `arroyo` (new) | Arroyo | Depends on Tansu (wave 3) for Kafka-compatible topics |
| 4 | `parquet-compactor` (existing) | Parquet Compactor | Unchanged |

> **Wave 2 → Wave 3 dependency**: The OTel Collector's `otlp/tansu` exporter targets `tansu-broker.monitoring.svc.cluster.local:4317`. Since the collector has `retry_on_failure` with `max_elapsed_time: 300s`, and Tansu deploys at wave 3 (seconds after wave 2), the exporter will buffer and retry until Tansu is ready. No special ordering is needed.

### 5.4 App of Apps Discovery

All new ArgoCD Application YAMLs in `apps/` are automatically discovered by the root Application (which uses `directory.recurse: true` and `directory.recurse: true`). No changes to the root Application are needed.

---

## 6. Resource Budget

### 6.1 Per-Node: OTel Collector DaemonSet

| Resource | Before (metrics only) | After (metrics + logs) | Delta |
|---|---|---|---|
| CPU request | 100m | 150m | +50m |
| CPU limit | — | 500m | new |
| Memory request | 128Mi | 200Mi | +72Mi |
| Memory limit | 512Mi | 600Mi | +88Mi |
| memory_limiter limit_mib | 400 | 500 | +100Mi |
| memory_limiter spike_limit_mib | 100 | 150 | +50Mi |

**Notes:**
- The 600Mi limit provides headroom above the 500Mi `memory_limiter` (500Mi soft limit + 100Mi safety gap for OTel runtime overhead).
- The `filelog` receiver uses minimal memory (tail + parse); the `transform` processor is the main memory consumer.
- If nodes run >80 pods, consider increasing to 700Mi limit.

### 6.2 Cluster-Wide: New Components

| Component | Replicas | CPU Request | CPU Limit | Memory Request | Memory Limit | PVC |
|---|---|---|---|---|---|---|
| Tansu Broker | 1 | 100m | 500m | 256Mi | 512Mi | 5Gi |
| PostgreSQL (for Tansu) | 1 | 50m | 250m | 128Mi | 256Mi | 2Gi |
| Trino | 1 | 250m | 1000m | 512Mi | 1Gi | — |
| Arroyo | 1 | 100m | 500m | 256Mi | 512Mi | — |

### 6.3 Total Cluster Overhead

| Category | CPU Request | CPU Limit | Memory Request | Memory Limit |
|---|---|---|---|---|
| OTel Collector (per node, ×N nodes) | 150m × N | 500m × N | 200Mi × N | 600Mi × N |
| New components (fixed) | 500m | 2250m | 1152Mi | 2304Mi |
| **Total (4-node cluster)** | **1100m** | **4250m** | **1952Mi** | **4704Mi** |

---

## 7. Rollback Plan

### 7.1 Rollback Decision Tree

```
Problem detected?
├── Is the metrics pipeline affected?
│   ├── YES → Immediate rollback (Priority 1)
│   │   └── Remove logs pipeline from collector.yaml → ArgoCD sync
│   └── NO → Continue diagnosis
├── Are collector pods OOMKilling?
│   ├── YES → Increase memory limits or remove logs pipeline
│   │   └── Option A: bump limit to 700Mi
│   │   └── Option B: remove logs pipeline entirely
│   └── NO → Continue diagnosis
├── Is Tansu failing?
│   ├── YES → Scale Tansu to 0; collector exporter will retry and drop
│   │   └── FluentBit pipeline is unaffected
│   └── NO → Continue diagnosis
└── Is data quality poor (wrong severity, split multiline)?
    └── Adjust transform processor OTTL and redeploy
```

### 7.2 Specific Rollback Steps

**Step 1: Disable OTel logs pipeline (no cluster downtime)**

Edit `infrastructure/monitoring/otel-collector/collector.yaml` and remove:
- The `filelog` receiver definition
- The `logs` pipeline from `service.pipelines`
- The `otlp/tansu` exporter
- The `transform/normalize-logs` processor
- The `batch/logs` processor
- The `file_storage` extension
- The extra volumes and volumeMounts

Commit and push. ArgoCD syncs the change within ~3 minutes. The metrics pipeline continues uninterrupted.

```bash
# Verify metrics pipeline still works
kubectl -n monitoring port-forward svc/otel-gateway-collector 8888:8888
curl -s http://localhost:8888/metrics | grep otelcol_receiver_accepted_metric_points
```

**Step 2: Scale Tansu to 0 (optional, to reclaim resources)**

```bash
kubectl -n monitoring scale statefulset tansu-broker --replicas=0
```

**Step 3: Remove ArgoCD Applications (full cleanup)**

Delete the new Application files from `apps/`:

```bash
rm apps/tansu.yaml apps/iceberg-query.yaml apps/arroyo.yaml
git commit -am "rollback: remove log-normalization ArgoCD apps"
git push
```

ArgoCD with `prune: true` will delete all cluster resources managed by these Applications.

**Step 4: Verify FluentBit pipeline is untouched**

```bash
# Check FluentBit DaemonSet is running
kubectl -n monitoring get ds fluentbit-daemonset

# Check FluentBit Aggregator is running
kubectl -n monitoring get deploy fluentbit-aggregator

# Verify Parquet files are still being written
aws s3 ls s3://logs/raw/containers/ --recursive | tail -5
```

### 7.3 What NOT to Rollback

| Component | Why not | Action |
|---|---|---|
| FluentBit DaemonSet | Was never modified | No action needed |
| FluentBit Aggregator | Was never modified | No action needed |
| OTel Gateway | Was never modified | No action needed |
| OTel Operator | Was never modified | No action needed |
| S3 bucket / Parquet data | New data only | No action needed; Iceberg data in S3 can be left or deleted manually |

### 7.4 Rollback Verification Checklist

- [ ] OTel Collector DaemonSet running with metrics pipeline only
- [ ] Prometheus receiving metrics via OTel Gateway
- [ ] FluentBit DaemonSet running (check pod logs for forwarding)
- [ ] FluentBit Aggregator writing Parquet to S3
- [ ] No OOMKilled pods in monitoring namespace
- [ ] ArgoCD Applications for tansu, iceberg-query, arroyo removed (if full cleanup)
- [ ] Resource usage on nodes returns to pre-change levels

---

## 8. Implementation Order

### Phase 1: Foundation (Tansu + PostgreSQL)

| Step | Action | Dependency | Verification |
|---|---|---|---|
| 1.1 | Create `infrastructure/tansu/` directory with all YAML manifests | None | Files exist in repo |
| 1.2 | Create `apps/tansu.yaml` ArgoCD Application | Step 1.1 | ArgoCD shows tansu app |
| 1.3 | Create Kubernetes Secret with S3 credentials and PostgreSQL DSN | None | `kubectl -n monitoring get secret tansu-secrets` |
| 1.4 | Deploy PostgreSQL StatefulSet (if dedicated) | Step 1.3 | `kubectl -n monitoring get pods -l app.kubernetes.io/name=tansu-pg` → Running |
| 1.5 | Deploy Tansu StatefulSet | Steps 1.3, 1.4 | `kubectl -n monitoring get pods -l app.kubernetes.io/name=tansu` → Running; verify ports 4317 and 9092 are listening |

### Phase 2: OTel Collector Logs Pipeline

| Step | Action | Dependency | Verification |
|---|---|---|---|
| 2.1 | Update `infrastructure/monitoring/otel-collector/collector.yaml` with logs pipeline config | Step 1.5 (Tansu must be up for OTLP export) | Git diff shows new receivers, processors, exporters, pipelines |
| 2.2 | Commit and push; ArgoCD auto-syncs | Step 2.1 | `kubectl -n monitoring get pods -l app.kubernetes.io/name=collector-daemonset` → Running with both metrics and logs pipelines |
| 2.3 | Verify collector pods are not OOMKilling | Step 2.2 | `kubectl -n monitoring get events --field-selector reason=OOMKilling` → empty for 30 minutes |
| 2.4 | Verify logs are arriving at Tansu | Steps 2.2, 1.5 | Check Tansu pod logs for received OTLP records |

### Phase 3: Query Layer

| Step | Action | Dependency | Verification |
|---|---|---|---|
| 3.1 | Create `infrastructure/iceberg-query/` directory with Trino manifests | None | Files exist in repo |
| 3.2 | Create `apps/iceberg-query.yaml` ArgoCD Application | Step 3.1 | ArgoCD shows iceberg-query app |
| 3.3 | Deploy Trino | Steps 1.5, 3.1 | `kubectl -n monitoring get pods -l app.kubernetes.io/name=trino` → Running; `curl http://trino:8080/v1/info` → healthy |
| 3.4 | Validate Iceberg table exists and is queryable | Step 3.3 | Execute Q1-Q5 via Trino CLI; results returned |
| 3.5 | Validate DuckDB ad-hoc queries | Step 3.4 | Execute same queries via local DuckDB CLI; results match Trino |

### Phase 4: Streaming SQL (Arroyo)

| Step | Action | Dependency | Verification |
|---|---|---|---|
| 4.1 | Create `infrastructure/arroyo/` directory with manifests | None | Files exist in repo |
| 4.2 | Create `apps/arroyo.yaml` ArgoCD Application | Step 4.1 | ArgoCD shows arroyo app |
| 4.3 | Deploy Arroyo | Step 1.5 | `kubectl -n monitoring get pods -l app.kubernetes.io/name=arroyo` → Running |
| 4.4 | Configure streaming SQL pipeline for second-pass normalization | Step 4.3 | Arroyo UI shows active pipeline |
| 4.5 | Validate derived table (`otel_logs_normalized`) | Steps 4.4, 3.3 | Query derived table via Trino |

### Phase 5: Validation

| Step | Action | Dependency | Verification |
|---|---|---|---|
| 5.1 | Run acceptance criteria AC-1 through AC-17 | All phases | Each criterion passes (see spec.md) |
| 5.2 | 24-hour observation period | Step 5.1 | No OOMKills, log counts within ±5% of FluentBit |
| 5.3 | Document operational runbook | Step 5.2 | Runbook covers: query examples, troubleshooting, scaling |

---

## 9. File Change Summary

| File | Action | Description |
|---|---|---|
| `infrastructure/monitoring/otel-collector/collector.yaml` | **Modified** | Add logs pipeline, filelog receiver, transform processor, otlp/tansu exporter, volumes, extensions, updated resource limits |
| `infrastructure/tansu/statefulset.yaml` | **New** | Tansu Broker StatefulSet |
| `infrastructure/tansu/service.yaml` | **New** | Tansu ClusterIP Service (OTLP + Kafka) |
| `infrastructure/tansu/configmap.yaml` | **New** | Tansu configuration (if using config file) |
| `infrastructure/tansu/secret.yaml` | **New** | S3 credentials + PostgreSQL DSN |
| `infrastructure/tansu/postgres-statefulset.yaml` | **New** | Optional dedicated PostgreSQL StatefulSet |
| `infrastructure/tansu/postgres-service.yaml` | **New** | PostgreSQL Service |
| `infrastructure/iceberg-query/trino-deployment.yaml` | **New** | Trino Deployment |
| `infrastructure/iceberg-query/trino-service.yaml` | **New** | Trino Service |
| `infrastructure/iceberg-query/trino-catalog-configmap.yaml` | **New** | Trino Iceberg catalog config |
| `infrastructure/arroyo/deployment.yaml` | **New** | Arroyo Deployment |
| `infrastructure/arroyo/service.yaml` | **New** | Arroyo Service |
| `apps/tansu.yaml` | **New** | ArgoCD Application for Tansu |
| `apps/iceberg-query.yaml` | **New** | ArgoCD Application for Trino |
| `apps/arroyo.yaml` | **New** | ArgoCD Application for Arroyo |

**Total**: 1 modified file, 14 new files.

---

## 10. Open Design Decisions

| # | Decision | Current Choice | Risk if Wrong | Mitigation |
|---|---|---|---|---|
| D1 | Tansu image tag | `ghcr.io/rustyconover/tansu:latest` | Breaking change in upstream | Pin to specific tag after first successful deployment |
| D2 | Tansu Iceberg write mode | Native Iceberg via `--all-features` flag | Tansu's Iceberg support may be immature | Fallback: write to Kafka topic, use separate Iceberg Sink Connector |
| D3 | REST catalog vs Hadoop catalog | REST catalog | REST catalog may not be available in Tansu | Hadoop catalog is simpler — works directly with S3 path, no separate catalog service needed |
| D4 | PostgreSQL vs SQLite | PostgreSQL | Over-engineering for a homelab | SQLite is simpler; start with SQLite, migrate to PostgreSQL if durability issues arise |
| D5 | Multiline regex breadth | Broad `line_start_pattern` | Overly broad pattern merges unrelated lines | Monitor first 24h for false merges; tighten regex if needed |
| D6 | Separate `batch/logs` vs shared `batch` | Separate `batch/logs` with 5s timeout | More processor overhead | Benefit: logs get lower latency (5s vs 10s) without affecting metrics batch behavior |
