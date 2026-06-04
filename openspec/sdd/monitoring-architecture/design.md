# Design: monitoring-architecture

**Change:** monitoring-architecture  
**Phase:** design  
**Stacks affected:** `infrastructure`, `apps`, `bootstrap`  
**Status:** draft

---

## 1. File Structure

The project uses plain Kubernetes YAML manifests (no Helm, no Kustomize). The OTel Operator is deployed from upstream manifests via the ArgoCD Application pointing at the official GitHub release tarball. The `OpenTelemetryCollector` CRDs are applied as plain YAML that the Operator renders at runtime.

### 1.1 Repository Layout

```
homelab/
├── apps/
│   ├── infrastructure-base.yaml          # (existing)
│   ├── otel-operator.yaml                # ArgoCD Application → wave 1
│   ├── otel-collector.yaml               # ArgoCD Application → wave 2
│   ├── otel-gateway.yaml                 # ArgoCD Application → wave 3
│   └── fluentbit.yaml                    # ArgoCD Application → wave 3
│
└── infrastructure/
    └── monitoring/
        ├── namespace.yaml                # monitoring namespace
        ├── otel-operator/
        │   └── (empty — upstream manifest applied via ArgoCD remote URL)
        ├── otel-collector/
        │   ├── collector.yaml            # OpenTelemetryCollector CR (DaemonSet)
        │   └── rbac.yaml                 # ServiceAccount + ClusterRole + ClusterRoleBinding
        ├── otel-gateway/
        │   ├── collector.yaml            # OpenTelemetryCollector CR (StatefulSet + TA)
        │   └── rbac.yaml                 # ServiceAccount + ClusterRole + ClusterRoleBinding
        └── fluentbit/
            ├── configmap.yaml            # Fluent Forward input + S3 output config
            ├── deployment.yaml           # Fluentbit Deployment (1 replica)
            ├── service.yaml              # ClusterIP Service for Fluent Forward port 24224
            └── secret.yaml               # Garage S3 credentials (gitignored — template below)
```

### 1.2 Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Kustomize vs plain YAML | **Plain YAML** | The project has no Kustomize dependency. Adding it for 5 manifests introduces tooling overhead with no benefit. ArgoCD `directory.recurse: true` handles discovery. |
| OTel Operator source | **ArgoCD remote URL** (upstream `opentelemetry-operator.yaml` from GitHub releases) | The upstream manifest bundles CRDs, Deployment, webhook, and RBAC. No need to vendor it in the repo. ArgoCD fetches it on sync. |
| OpenTelemetryCollector CRs as plain YAML | **Yes** | The OTel Operator reconciles `OpenTelemetryCollector` custom resources into DaemonSets/StatefulSets. The CR is a standard Kubernetes manifest. |
| Separate ArgoCD Application per component | **Yes** (4 apps: operator, collector, gateway, fluentbit) | Enables independent sync waves, independent pruning, and independent health checks. Aligns with App of Apps pattern. |

### 1.3 ArgoCD Application Pattern

All applications follow this template:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: <component>
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "<1|2|3>"
spec:
  project: default
  source:
    repoURL: https://github.com/manudiv16/homelab.git
    targetRevision: main
    path: infrastructure/monitoring/<component>
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring          # or opentelemetry-operator-system for the operator
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

### 1.4 Gitignore Additions

```gitignore
# Monitoring secrets — never commit Garage S3 credentials
infrastructure/monitoring/fluentbit/secret.yaml
```

---

## 2. Detailed OTel Configuration

### 2.1 Namespace

**File:** `infrastructure/monitoring/namespace.yaml`

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: monitoring
```

---

### 2.2 OTel Operator (Wave 1)

**File:** `apps/otel-operator.yaml`

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: otel-operator
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "1"
spec:
  project: default
  source:
    # Upstream operator manifest from the official OTel Operator releases
    # This URL points to the latest stable release manifest bundle
    # (CRDs + Deployment + RBAC + Webhook)
    repoURL: https://github.com/open-telemetry/opentelemetry-operator.git
    targetRevision: v0.122.0
    path: config/default
    kustomize: {}
  destination:
    server: https://kubernetes.default.svc
    namespace: opentelemetry-operator-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

> **Note:** The upstream OTel Operator repo ships Kustomize bases under `config/`. ArgoCD can render kustomize natively via the `kustomize: {}` key even when the rest of the repo uses plain YAML. This avoids vendoring the full operator manifest. The `config/default` base includes all CRDs, the webhook, cert-manager integration, and RBAC.

> **Alternative (simpler):** If kustomize on a remote repo is problematic, pin a specific upstream release tarball URL and use `directory: {}`:
> ```yaml
> source:
>   repoURL: https://github.com/open-telemetry/opentelemetry-operator.git
>   targetRevision: v0.122.0
>   path: config/manager   # smaller subset; CRDs via separate sync
> ```
> Or vendor the full rendered manifest as a single YAML file in `infrastructure/monitoring/otel-operator/operator.yaml`.

> **Prerequisite:** cert-manager MUST be installed. The OTel Operator webhook requires cert-manager to provision its TLS certificate. If cert-manager is not already deployed, it must be installed first (either as a separate ArgoCD Application or manually). This is outside the scope of this change.

---

### 2.3 OTel Collector DaemonSet (Wave 2)

#### 2.3.1 RBAC

**File:** `infrastructure/monitoring/otel-collector/rbac.yaml`

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: otel-collector-daemonset
  namespace: monitoring
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: otel-collector-daemonset
rules:
  - apiGroups: [""]
    resources: ["pods", "namespaces", "nodes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: otel-collector-daemonset
subjects:
  - kind: ServiceAccount
    name: otel-collector-daemonset
    namespace: monitoring
roleRef:
  kind: ClusterRole
  name: otel-collector-daemonset
  apiGroup: rbac.authorization.k8s.io
```

#### 2.3.2 OpenTelemetryCollector CR (DaemonSet)

**File:** `infrastructure/monitoring/otel-collector/collector.yaml`

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
      cpu: 100m
      memory: 128Mi
    limits:
      memory: 512Mi
  # Host network not required; filelog reads hostPath volume
  volumes:
    - name: varlogpods
      hostPath:
        path: /var/log/pods
        type: Directory
    - name: varlog
      hostPath:
        path: /var/log
        type: Directory
  volumeMounts:
    - name: varlogpods
      mountPath: /var/log/pods
      readOnly: true
    - name: varlog
      mountPath: /var/log
      readOnly: true
  config: |
    receivers:
      hostmetrics:
        collection_interval: 30s
        scrapers:
          cpu:
          memory:
          disk:
          network:
      kubeletstats:
        collection_interval: 30s
        auth_type: serviceAccount
        endpoint: "${env:K8S_NODE_NAME}:10250"
        # Insecure for homelab — kubelet on LXC does not have valid cert
        # for the node IP. serviceAccount auth still validates the token.
        insecure_skip_verify: true
      filelog:
        include:
          - /var/log/pods/*/*/*.log
        operators:
          - type: container
            id: parser-containers
            output: move_body
          - type: move
            id: move_body
            from: attributes.log
            to: body

    processors:
      memory_limiter:
        check_interval: 5s
        limit_mib: 400
        spike_limit_mib: 100
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

    exporters:
      otlp:
        endpoint: "otel-gateway-collector:4317"
        tls:
          insecure: true

    service:
      pipelines:
        metrics:
          receivers: [hostmetrics, kubeletstats]
          processors: [memory_limiter, k8sattributes, resource/cluster, batch]
          exporters: [otlp]
        logs:
          receivers: [filelog]
          processors: [memory_limiter, k8sattributes, resource/cluster, batch]
          exporters: [otlp]
      telemetry:
        metrics:
          address: "0.0.0.0:8888"
```

> **Note on `K8S_NODE_NAME`:** The OTel Operator injects this environment variable automatically from the `spec.nodeName` when `mode: daemonset`. The kubelet API endpoint on k3s LXC nodes is `https://<node-IP>:10250`. Using `${env:K8S_NODE_NAME}` resolves to the node hostname, which k3s resolves correctly.

> **Note on `kubeletstats` endpoint:** The endpoint format depends on how k3s exposes the kubelet. On LXC nodes the kubelet typically binds to the node's IP on port 10250. If `K8S_NODE_NAME` resolves to the hostname (not the IP), the endpoint may need to be `https://192.168.4.20x:10250` hardcoded or use the downward API to inject the node IP. This must be verified during the apply phase.

---

### 2.4 OTel Collector Gateway (Wave 3)

#### 2.4.1 RBAC

**File:** `infrastructure/monitoring/otel-gateway/rbac.yaml`

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: otel-collector-gateway
  namespace: monitoring
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: otel-collector-gateway
rules:
  - apiGroups: [""]
    resources: ["pods", "namespaces", "nodes", "endpoints", "services"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: otel-collector-gateway
subjects:
  - kind: ServiceAccount
    name: otel-collector-gateway
    namespace: monitoring
roleRef:
  kind: ClusterRole
  name: otel-collector-gateway
  apiGroup: rbac.authorization.k8s.io
```

#### 2.4.2 OpenTelemetryCollector CR (StatefulSet + TargetAllocator)

**File:** `infrastructure/monitoring/otel-gateway/collector.yaml`

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: collector-gateway
  namespace: monitoring
spec:
  mode: statefulset
  replicas: 1
  serviceAccount: otel-collector-gateway
  image: otel/opentelemetry-collector-contrib:0.123.0
  resources:
    requests:
      cpu: 250m
      memory: 256Mi
    limits:
      memory: 1Gi
  # TargetAllocator configuration for annotation-driven pod scrape
  targetAllocator:
    enabled: true
    image: ghcr.io/open-telemetry/opentelemetry-operator/target-allocator:0.122.0
    replicas: 1
    resources:
      requests:
        cpu: 50m
        memory: 64Mi
      limits:
        memory: 128Mi
    # Discover pods with prometheus.io/scrape: "true" annotations
    prometheusCR:
      enabled: true
  config: |
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318
      # The prometheus receiver scrape config is managed by the TargetAllocator.
      # The placeholder job definition is replaced with dynamic targets at runtime.
      prometheus:
        config:
          scrape_configs:
            - job_name: 'otel-gateway'
              scrape_interval: 30s

    processors:
      memory_limiter:
        check_interval: 5s
        limit_mib: 800
        spike_limit_mib: 200
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

    exporters:
      fluentforward:
        endpoint: "fluentbit-aggregator:24224"
      prometheusremotewrite:
        endpoint: "http://__PROMETHEUS_IP__:9090/api/v1/write"
        # No auth for homelab simplicity. Prometheus is on a trusted LAN.
        # If basic auth is enabled on Prometheus, add:
        #   auth:
        #     authenticator: basicauth/prw
        # tls:
        #   insecure: true

    service:
      pipelines:
        metrics:
          receivers: [otlp, prometheus]
          processors: [memory_limiter, k8sattributes, resource/cluster, batch]
          exporters: [prometheusremotewrite]
        logs:
          receivers: [otlp]
          processors: [memory_limiter, k8sattributes, resource/cluster, batch]
          exporters: [fluentforward]
      telemetry:
        metrics:
          address: "0.0.0.0:8888"
```

> **`__PROMETHEUS_IP__` placeholder:** Replace with the actual Prometheus VM/LXC IP address (determined during provisioning). This is a manual substitution since the project has no Kustomize overlays or Helm templating. Example: `http://192.168.4.85:9090/api/v1/write`.

> **TargetAllocator:** The `targetAllocator.prometheusCR.enabled: true` causes the TargetAllocator to watch for `ServiceMonitor` and `PodMonitor` CRDs (if installed) and inject scrape targets into the `prometheus` receiver. If the Prometheus CRDs are not installed, use `targetAllocator.allocationStrategy: least-weighted` with the annotation-based discovery instead. Verify this during apply.

> **Fluent Forward exporter:** The `fluentforward` exporter sends logs in Fluent Forward protocol (msgpack) to Fluentbit on port 24224. This is a native OTel Collector contrib exporter. The Fluentbit Service name `fluentbit-aggregator` must resolve within the cluster (provided by the Service created in §3).

---

## 3. Fluentbit Configuration

### 3.1 Deployment

**File:** `infrastructure/monitoring/fluentbit/deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fluentbit-aggregator
  namespace: monitoring
  labels:
    app.kubernetes.io/name: fluentbit-aggregator
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: fluentbit-aggregator
  template:
    metadata:
      labels:
        app.kubernetes.io/name: fluentbit-aggregator
    spec:
      containers:
        - name: fluentbit
          image: fluent/fluent-bit:4.1   # v4.1+ required for Parquet S3 output
          ports:
            - containerPort: 24224
              name: fluent-forward
              protocol: TCP
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              memory: 512Mi
          volumeMounts:
            - name: config
              mountPath: /fluent-bit/etc
            - name: buffers
              mountPath: /var/fluent-bit/buffers
          env:
            - name: GARAGE_S3_ENDPOINT
              value: "http://__GARAGE_IP__:3900"
            - name: GARAGE_BUCKET
              value: "logs"
          envFrom:
            - secretRef:
                name: garage-credentials
      volumes:
        - name: config
          configMap:
            name: fluentbit-aggregator-config
        - name: buffers
          emptyDir: {}
```

### 3.2 Service

**File:** `infrastructure/monitoring/fluentbit/service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: fluentbit-aggregator
  namespace: monitoring
  labels:
    app.kubernetes.io/name: fluentbit-aggregator
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: fluentbit-aggregator
  ports:
    - port: 24224
      targetPort: 24224
      protocol: TCP
      name: fluent-forward
```

### 3.3 ConfigMap

**File:** `infrastructure/monitoring/fluentbit/configmap.yaml`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluentbit-aggregator-config
  namespace: monitoring
data:
  fluent-bit.conf: |
    # ==============================================================
    # Fluentbit Aggregator Configuration
    # Receives logs from OTel Gateway via Fluent Forward
    # Writes Parquet files to Garage (S3) with Hive partitioning
    # ==============================================================

    [SERVICE]
        flush           5
        daemon          off
        log_level       info
        parsers_file    parsers.conf
        plugins_file    plugins.conf
        http_server     on
        http_listen     0.0.0.0
        http_port       2020
        # Storage for buffering before S3 upload
        storage.path              /var/fluent-bit/buffers
        storage.sync              normal
        storage.checksum          off
        storage.backlog.mem_limit 50M

    # ---- Input: Fluent Forward from OTel Gateway ----
    [INPUT]
        Name              forward
        Listen            0.0.0.0
        Port              24224
        Tag               logs.*
        Buffer_Chunk_Size 1M
        Buffer_Max_Size   5M

    # ---- Filter: Add Hive partition keys from timestamp ----
    # The OTel Gateway enriches logs with k8s metadata.
    # Fluent Forward delivers them as structured records.
    # We extract year/month/day/hour for Hive partitioning.
    [FILTER]
        Name          modify
        Match         logs.*
        # Ensure timestamp exists; Fluentbit uses record timestamp

    # ---- Output: S3 (Garage) with Parquet format ----
    [OUTPUT]
        Name                  s3
        Match                 logs.*
        bucket                ${GARAGE_BUCKET}
        region                garage
        endpoint              ${GARAGE_S3_ENDPOINT}
        tls                   off
        tls.verify            off
        use_put_object        On
        compression           none
        # Parquet format — requires Fluentbit v4.1+
        Format                parquet
        # Hive-style partitioning
        s3_key_format         /raw/containers/year=%Y/month=%m/day=%d/hour=%H/$UUID.parquet
        s3_key_format_tag_delimiters .
        # Buffering: flush every 5MB or every 5 minutes
        total_file_size       5M
        upload_timeout        5m
        # Storage: use filesystem buffering for durability
        storage.type          filesystem
        # S3 credentials from environment (injected via Secret)
        # FLUENT_S3_ACCESS_KEY_ID and FLUENT_S3_SECRET_ACCESS_KEY
        # are read from the environment automatically by the S3 plugin

  parsers.conf: |
    # No custom parsers needed — OTel Gateway sends structured Fluent Forward records

  plugins.conf: |
    # No additional plugin loading needed
```

> **S3 credentials:** The Fluentbit S3 plugin reads credentials from the environment variables `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (or `FLUENT_S3_ACCESS_KEY_ID` / `FLUENT_S3_SECRET_ACCESS_KEY`). These are provided by the Kubernetes Secret referenced in the Deployment's `envFrom`.

### 3.4 Secret (template — do not commit)

**File:** `infrastructure/monitoring/fluentbit/secret.yaml` (gitignored)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: garage-credentials
  namespace: monitoring
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: "<GARAGE_ACCESS_KEY>"
  AWS_SECRET_ACCESS_KEY: "<GARAGE_SECRET_KEY>"
```

> **Provisioning:** After Garage is installed, extract the S3 credentials from `~/garage.creds` on the Garage LXC. Replace the placeholders and apply this Secret manually:
> ```bash
> kubectl apply -f infrastructure/monitoring/fluentbit/secret.yaml
> ```

---

## 4. Garage Setup Notes

Garage is an S3-compatible object storage service written in Rust. It runs as a lightweight Alpine LXC on Proxmox, provisioned via community-scripts.

### 4.1 Provisioning

On the Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/garage.sh)"
```

**Community-scripts defaults for Garage:**
- OS: Alpine Linux
- CPU: 1 vCPU
- RAM: 512 MB
- Disk: 5 GB
- Ports: 3900 (S3 API), 3902 (Web UI), 3903 (Admin API)

### 4.2 Post-Install Configuration

After the LXC is created and Garage is running:

**Step 1: Get credentials**

```bash
# On the Garage LXC
cat ~/garage.creds
# Output contains access_key_id and secret_access_key
```

**Step 2: Create the `logs` bucket**

```bash
# On the Garage LXC (Admin API)
garage bucket create logs

# Create an S3 API key
garage key create monitoring

# Allow the key to access the bucket
garage bucket allow --key monitoring --read --write logs

# Get the S3 credentials for the key
garage key info monitoring
```

**Step 3: Note the LXC IP address**

```bash
# On the Garage LXC
ip -4 addr show | grep inet
# Record the IP — needed for Fluentbit GARAGE_S3_ENDPOINT and Grafana DuckDB config
```

The IP is used in two places:
- Fluentbit `GARAGE_S3_ENDPOINT` (e.g., `http://192.168.4.84:3900`)
- Grafana DuckDB S3 configuration

### 4.3 Garage Configuration Reference

| Property | Value |
|----------|-------|
| LXC IP | `<GARAGE_IP>` (determined at install time) |
| S3 API | `http://<GARAGE_IP>:3900` |
| Web UI | `http://<GARAGE_IP>:3902` |
| Admin API | `http://<GARAGE_IP>:3903` |
| Config path | `/etc/garage.toml` |
| Data path | `/var/lib/garage/data` (default) |
| Bucket | `logs` |
| Region (for S3 clients) | `garage` (Garage uses this as its region identifier) |
| Access Key | from `~/garage.creds` |
| Secret Key | from `~/garage.creds` |

### 4.4 S3 Bucket Structure (Expected)

After Fluentbit runs for a while, the `logs` bucket will contain:

```
s3://logs/
└── raw/
    └── containers/
        ├── year=2026/
        │   ├── month=06/
        │   │   ├── day=05/
        │   │   │   ├── hour=14/
        │   │   │   │   ├── a1b2c3d4-e5f6-7890-abcd-ef1234567890.parquet
        │   │   │   │   └── ...
        │   │   │   └── hour=15/
        │   │   │       └── ...
        │   │   └── day=06/
        │   │       └── ...
        │   └── month=07/
        │       └── ...
        └── year=2027/
            └── ...
```

DuckDB queries use the Hive partition columns (`year`, `month`, `day`, `hour`) for partition pruning.

---

## 5. Prometheus Configuration

Prometheus runs on a Proxmox VM or LXC. It receives metrics via `remote_write` from the OTel Gateway. It does **not** scrape any targets.

### 5.1 Installation

On a Proxmox VM or LXC:

```bash
# Using Docker (simplest)
docker run -d \
  --name prometheus \
  --restart unless-stopped \
  -p 9090:9090 \
  -v /etc/prometheus:/etc/prometheus \
  -v prometheus-data:/prometheus \
  prom/prometheus:latest \
  --config.file=/etc/prometheus/prometheus.yml \
  --web.enable-remote-write-receiver \
  --storage.tsdb.retention.time=30d \
  --storage.tsdb.retention.size=5GB
```

> **Alternative:** Install Prometheus natively via Proxmox community-scripts or Ansible. Docker is shown for simplicity.

### 5.2 Prometheus Configuration

**File:** `/etc/prometheus/prometheus.yml` (on the Prometheus VM)

```yaml
# Prometheus configuration for push-only mode
# All metrics arrive via remote_write from the OTel Gateway
# No scrape_configs — OTel always pushes

global:
  scrape_interval: 30s     # Used for PromQL step defaults, not for scraping
  evaluation_interval: 30s

# No scrape_configs section — intentionally omitted
# All data arrives via the remote_write receiver API
```

### 5.3 Startup Flags

| Flag | Value | Purpose |
|------|-------|---------|
| `--web.enable-remote-write-receiver` | (set) | Enables the `/api/v1/write` endpoint to accept remote_write pushes |
| `--storage.tsdb.retention.time` | `30d` | 30-day retention for homelab |
| `--storage.tsdb.retention.size` | `5GB` | Cap storage usage |
| `--web.listen-address` | `0.0.0.0:9090` | Listen on all interfaces (default) |

### 5.4 Network Access

Prometheus must be reachable from k3s cluster nodes:
- Port: `9090` (HTTP, no TLS)
- No authentication (homelab, trusted LAN)
- OTel Gateway → `http://<PROMETHEUS_IP>:9090/api/v1/write`

---

## 6. Grafana Configuration

Grafana runs on a Proxmox VM or LXC. It provides two datasources: Prometheus for metrics and DuckDB for Parquet log queries from Garage.

### 6.1 Installation

On a Proxmox VM or LXC:

```bash
# Using Docker (simplest)
docker run -d \
  --name grafana \
  --restart unless-stopped \
  -p 3000:3000 \
  -v grafana-data:/var/lib/grafana \
  -v /etc/grafana/provisioning:/etc/grafana/provisioning \
  -e GF_INSTALL_PLUGINS="https://github.com/motherduckdb/grafana-duckdb-datasource/releases/download/v1.2.0/motherduck-duckdb-datasource-1.2.0.zip;grafana-duckdb-datasource" \
  grafana/grafana:latest
```

> **Alternative:** Install Grafana natively via Proxmox community-scripts or Ansible.

### 6.2 Datasource Provisioning

**File:** `/etc/grafana/provisioning/datasources/datasources.yaml` (on the Grafana VM)

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://__PROMETHEUS_IP__:9090
    isDefault: true
    editable: true

  - name: Logs-DuckDB
    type: grafana-duckdb-datasource
    access: proxy
    jsonData:
      # DuckDB plugin uses a "connection string" with S3 extension
      # MotherDuck is NOT used — we use local DuckDB with S3 extension
      database: ":memory:"
      # Init SQL runs when the datasource initializes
      initQuery: |
        INSTALL httpfs;
        LOAD httpfs;
        SET s3_endpoint='__GARAGE_IP__:3900';
        SET s3_region='garage';
        SET s3_use_ssl=false;
        SET s3_url_style='path';
    secureJsonData:
      # S3 credentials for Garage
      # These are passed to DuckDB's S3 extension
      s3_access_key_id: "__GARAGE_ACCESS_KEY__"
      s3_secret_access_key: "__GARAGE_SECRET_KEY__"
    editable: true
```

> **Placeholder replacement:** Replace `__PROMETHEUS_IP__`, `__GARAGE_IP__`, `__GARAGE_ACCESS_KEY__`, and `__GARAGE_SECRET_KEY__` with actual values after provisioning. These are hardcoded because the project does not use Kustomize or Helm templating.

### 6.3 DuckDB Logs() Macro

Create a Grafana SQL panel using this macro for querying logs:

```sql
-- DuckDB logs() macro for querying Parquet files in Garage S3
-- Replace GARAGE_IP with the actual Garage LXC IP
CREATE OR REPLACE MACRO logs(
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP - INTERVAL '1 hour',
    end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    log_search VARCHAR DEFAULT ''
) AS TABLE (
    SELECT *
    FROM read_parquet(
        's3://logs/raw/containers/year=*/month=*/day=*/hour=*/*.parquet',
        hive_partitioning=true
    )
    WHERE
        CAST(
            year || '-' || month || '-' || day || ' ' || hour || ':00:00'
            AS TIMESTAMP
        ) BETWEEN start_time AND end_time
        AND (log_search = '' OR body ILIKE '%' || log_search || '%')
    ORDER BY timestamp DESC
    LIMIT 1000
);

-- Example usage:
SELECT * FROM logs(
    CURRENT_TIMESTAMP - INTERVAL '1 hour',
    CURRENT_TIMESTAMP,
    'error'
);
```

> **Note:** The `logs()` macro body structure depends on the actual Parquet schema produced by Fluentbit. The `body` and `timestamp` column names are inferred from OTel log record fields. Verify column names after the first logs arrive in Garage and adjust the macro accordingly.

### 6.4 DuckDB S3 Extension Notes

The Grafana DuckDB plugin initializes DuckDB with the `httpfs` extension for S3 access:

```sql
-- These run automatically via the initQuery in the datasource config
INSTALL httpfs;
LOAD httpfs;
SET s3_endpoint='__GARAGE_IP__:3900';
SET s3_region='garage';           -- Garage uses 'garage' as its region
SET s3_use_ssl=false;             -- Garage runs HTTP on the LAN
SET s3_url_style='path';          -- Garage uses path-style S3 URLs
```

**Additional optimizations (optional, in initQuery):**

```sql
-- Cache S3 responses for faster repeated queries
INSTALL cache_httpfs;
LOAD cache_httpfs;
SET cache_httpfs_max_cache_size='128MB';
```

---

## 7. Implementation Phases

### Phase 1: Proxmox Infrastructure (Manual)

| Step | Action | Verification |
|------|--------|-------------|
| 1.1 | Provision Garage LXC on Proxmox via community-scripts | LXC running, `garage` service active |
| 1.2 | Create `logs` bucket and S3 API key on Garage | `garage bucket list` shows `logs`; `garage key info monitoring` shows credentials |
| 1.3 | Record Garage IP, access key, secret key | Document in secure location (not in repo) |
| 1.4 | Verify S3 API from Proxmox host | `curl http://<GARAGE_IP>:3900` returns Garage response |

### Phase 2: In-Cluster — Monitoring Namespace + Operator (Wave 1)

| Step | Action | Verification |
|------|--------|-------------|
| 2.1 | Create `infrastructure/monitoring/namespace.yaml` | Push to repo |
| 2.2 | Create `apps/otel-operator.yaml` ArgoCD Application | Push to repo |
| 2.3 | Verify cert-manager is installed | `kubectl get pods -n cert-manager` shows running pods |
| 2.4 | Wait for ArgoCD to sync and Operator to be ready | `kubectl get crd opentelemetrycollectors.opentelemetry.io` succeeds |

### Phase 3: In-Cluster — OTel DaemonSet (Wave 2)

| Step | Action | Verification |
|------|--------|-------------|
| 3.1 | Create `infrastructure/monitoring/otel-collector/rbac.yaml` | Push to repo |
| 3.2 | Create `infrastructure/monitoring/otel-collector/collector.yaml` | Push to repo |
| 3.3 | Create `apps/otel-collector.yaml` ArgoCD Application (wave 2) | Push to repo |
| 3.4 | Verify DaemonSet pods running on all nodes | `kubectl get pods -n monitoring -l app.kubernetes.io/name=collector-daemonset-collector` shows 3 pods |
| 3.5 | Verify kubeletstats endpoint connectivity | Check Collector logs for kubelet connection errors |
| 3.6 | Verify filelog is reading container logs | Check Collector logs for filelog receiver activity |

### Phase 4: In-Cluster — OTel Gateway + Fluentbit (Wave 3)

| Step | Action | Verification |
|------|--------|-------------|
| 4.1 | Create `infrastructure/monitoring/otel-gateway/rbac.yaml` | Push to repo |
| 4.2 | Create `infrastructure/monitoring/otel-gateway/collector.yaml` with actual Prometheus IP | Push to repo |
| 4.3 | Create `apps/otel-gateway.yaml` ArgoCD Application (wave 3) | Push to repo |
| 4.4 | Create Fluentbit manifests (deployment, service, configmap, secret) | Push to repo (except secret) |
| 4.5 | Create `apps/fluentbit.yaml` ArgoCD Application (wave 3) | Push to repo |
| 4.6 | Apply the garage-credentials Secret manually | `kubectl apply -f infrastructure/monitoring/fluentbit/secret.yaml` |
| 4.7 | Verify Gateway StatefulSet running | `kubectl get statefulset -n monitoring collector-gateway-collector` shows 1/1 |
| 4.8 | Verify Fluentbit Deployment running | `kubectl get deployment -n monitoring fluentbit-aggregator` shows 1/1 |
| 4.9 | Verify Fluentbit receiving logs from Gateway | Check Fluentbit logs for incoming records |

### Phase 5: Prometheus VM (Manual)

| Step | Action | Verification |
|------|--------|-------------|
| 5.1 | Provision Prometheus VM/LXC on Proxmox | VM running |
| 5.2 | Install Prometheus with `--web.enable-remote-write-receiver` | `curl http://<PROMETHEUS_IP>:9090/-/healthy` returns 200 |
| 5.3 | Configure `prometheus.yml` (no scrape_configs) | Config matches design |
| 5.4 | Verify remote_write is receiving data | `curl http://<PROMETHEUS_IP>:9090/api/v1/query?query=up` returns results |

### Phase 6: Grafana VM (Manual)

| Step | Action | Verification |
|------|--------|-------------|
| 6.1 | Provision Grafana VM/LXC on Proxmox | VM running |
| 6.2 | Install Grafana with DuckDB plugin | Grafana accessible on port 3000 |
| 6.3 | Configure Prometheus datasource | Grafana can query Prometheus |
| 6.4 | Configure DuckDB datasource with S3 connection to Garage | Grafana can run DuckDB SQL |
| 6.5 | Create `logs()` macro in DuckDB | Log queries return Parquet data |

### Phase 7: End-to-End Verification

| Step | Action | Verification |
|------|--------|-------------|
| 7.1 | Verify ArgoCD sync status for all 4 monitoring apps | All `Synced` and `Healthy` |
| 7.2 | Verify metrics in Prometheus (hostmetrics, kubeletstats) | PromQL query returns recent data |
| 7.3 | Verify logs in Garage | S3 list objects on `logs` bucket shows Parquet files with Hive partitions |
| 7.4 | Verify Grafana → Prometheus | Metric dashboard renders data |
| 7.5 | Verify Grafana → DuckDB → Garage | Log query via DuckDB returns log records |
| 7.6 | Verify Kubernetes metadata in logs | Parquet records contain `k8s_namespace_name`, `k8s_pod_name` columns |

---

## 8. DuckDB Notes

### 8.1 Grafana DuckDB Plugin

- **Repository:** https://github.com/motherduckdb/grafana-duckdb-datasource
- **Installation:** Via `GF_INSTALL_PLUGINS` environment variable or Grafana CLI
- **Version:** Use latest stable (v1.2.0 or newer)
- **Plugin ID:** `grafana-duckdb-datasource`

### 8.2 DuckDB S3 Extension

DuckDB uses the `httpfs` extension to query Parquet files from S3-compatible storage:

```sql
INSTALL httpfs;
LOAD httpfs;
```

### 8.3 S3 Configuration for Garage

```sql
SET s3_endpoint='__GARAGE_IP__:3900';
SET s3_region='garage';
SET s3_use_ssl=false;
SET s3_url_style='path';
```

> **Why `s3_url_style='path'`:** Garage supports both virtual-hosted and path-style S3 URLs, but path-style is more reliable for homelab setups without DNS.

> **Why `s3_region='garage'`:** Garage uses `garage` as its default region identifier. This is required by the S3 protocol even though Garage doesn't have regions.

### 8.4 Querying Hive-Partitioned Parquet

```sql
-- Direct query with Hive partitioning
SELECT *
FROM read_parquet(
    's3://logs/raw/containers/year=*/month=*/day=*/hour=*/*.parquet',
    hive_partitioning=true
)
WHERE year = '2026' AND month = '06' AND day = '05'
LIMIT 100;
```

The `hive_partitioning=true` parameter tells DuckDB to parse the directory structure as partition columns (`year`, `month`, `day`, `hour`). This enables partition pruning — DuckDB only reads files matching the `WHERE` clause on partition columns.

### 8.5 logs() Macro

```sql
CREATE OR REPLACE MACRO logs(
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP - INTERVAL '1 hour',
    end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    log_search VARCHAR DEFAULT ''
) AS TABLE (
    SELECT *
    FROM read_parquet(
        's3://logs/raw/containers/year=*/month=*/day=*/hour=*/*.parquet',
        hive_partitioning=true
    )
    WHERE
        -- Convert Hive partition columns to a filterable timestamp
        strptime(year || month || day || hour, '%Y%m%d%H')
            BETWEEN start_time AND end_time
        AND (log_search = '' OR body ILIKE '%' || log_search || '%')
    ORDER BY (
        SELECT MAX(ts) FROM unnest([timestamp]) AS _(ts)
    ) DESC NULLS LAST
    LIMIT 1000
);
```

> **Note:** The exact macro depends on the Parquet column names produced by Fluentbit. After the first logs arrive, inspect the schema with `DESCRIBE SELECT * FROM read_parquet('s3://logs/raw/containers/.../*.parquet', hive_partitioning=true)` and adjust column names in the macro.

### 8.6 cache_httpfs Extension

For faster repeated queries, enable DuckDB's HTTP response cache:

```sql
INSTALL cache_httpfs;
LOAD cache_httpfs;
SET cache_httpfs_max_cache_size='128MB';
```

This caches S3 HTTP responses locally in the Grafana VM's filesystem, avoiding repeated downloads for the same Parquet files during dashboard refreshes.

### 8.7 Partition Pruning

Hive-style partitioning (`year=/month=/day=/hour=`) enables DuckDB to prune irrelevant time ranges at query time. For example:

```sql
-- This query only reads files for June 5, 2026 — all other partitions are skipped
SELECT * FROM logs(
    TIMESTAMP '2026-06-05 00:00:00',
    TIMESTAMP '2026-06-05 23:59:59'
);
```

At homelab scale (GB/day at most), partition pruning mainly helps with query latency. Even without pruning, DuckDB can scan small datasets quickly.

---

## 9. Decision Log

| # | Decision | Rationale | Alternatives Rejected |
|---|----------|-----------|----------------------|
| D1 | Plain YAML (no Kustomize) | Matches existing repo conventions. No new tooling dependency. | Kustomize overlays (k8s-hub-operations pattern) — adds complexity for 5 manifest files. |
| D2 | Separate ArgoCD Application per component (4 apps) | Independent sync waves, independent health, independent rollback. | Single monolithic Application — harder to debug per-component failures. |
| D3 | OTel Operator from upstream kustomize base | Avoids vendoring ~2000 lines of upstream YAML in the repo. ArgoCD renders kustomize natively. | Vendor the full operator.yaml — hard to update, maintenance burden. |
| D4 | `fluentforward` exporter (not `otlphttp`) | Native Fluent Forward protocol is optimized for bulk log delivery. Fluentbit's Forward input is battle-tested. | OTLP HTTP to Fluentbit OTLP input — adds OTLP parsing overhead in Fluentbit for no benefit. |
| D5 | Prometheus push-only (no scrape) | Simplifies Prometheus config. All collection is unified through OTel. | Traditional Prometheus scrape — requires ServiceMonitor CRDs, more in-cluster complexity. |
| D6 | Garage on Alpine LXC | Lightweight (512MB), runs outside cluster, survives cluster resets. S3-compatible API works with both Fluentbit and DuckDB. | MinIO (heavier), NFS for Parquet (no S3 API for DuckDB). |
| D7 | DuckDB for log querying | Low resource usage (runs in Grafana process). Direct S3 Parquet read. SQL familiar. No indexing overhead. | Loki (heavier, requires index), Elasticsearch (heavier, Java). |
| D8 | No authentication on Prometheus remote_write | Homelab LAN is trusted. Simplifies OTel Gateway config. | Basic auth / mTLS — adds credential management for no security benefit in a homelab. |

---

## 10. Risks and Mitigations (Design-Level)

| # | Risk | Design Mitigation |
|---|------|-------------------|
| R1 | cert-manager not installed | Documented as prerequisite. Phase 2 verifies cert-manager before proceeding. |
| R2 | kubeletstats endpoint incorrect for LXC nodes | `insecure_skip_verify: true` set. Endpoint format documented. Will be validated during apply. |
| R3 | Fluent Forward exporter not in OTel contrib | Using `otel/opentelemetry-collector-contrib:0.123.0` which includes `fluentforward` exporter. Verified in upstream component list. |
| R4 | Parquet column names unknown until first logs arrive | DuckDB `logs()` macro documented as template. Explicit step in Phase 7 to inspect schema and adjust. |
| R5 | Garage IP not statically assigned | Documented as DHCP by default from community-scripts. Recommend reserving DHCP lease or configuring static IP on the LXC. |
| R6 | Fluentbit S3 plugin `format parquet` not available | Pinned to `fluent/fluent-bit:4.1` which includes Parquet support. If unavailable, fallback to JSON format with post-processing via DuckDB. |
