# Monitoring Architecture Specification

## Purpose

Deploy a unified observability stack for the homelab k3s cluster using OpenTelemetry for collection, Prometheus for metric storage, Parquet-on-S3 for log storage (Garage), and Grafana for visualization. All in-cluster components SHALL be managed via ArgoCD App of Apps. External components (Garage, Prometheus, Grafana) run on Proxmox LXC/VM and are configured manually.

---

## Requirements

### Requirement: OpenTelemetry Operator Installation

The system MUST deploy the OpenTelemetry Operator via upstream manifests as an ArgoCD Application in sync wave 1. The Operator SHALL provide the `OpenTelemetryCollector` CRD. The Operator deployment MUST depend on cert-manager being available in the cluster.

#### Scenario: Operator CRD available

- GIVEN cert-manager is installed and healthy in the cluster
- WHEN the ArgoCD Application for the OTel Operator syncs (wave 1)
- THEN the `opentelemetrycollectors.opentelemetry.io` CRD MUST be present in the cluster
- AND the Operator deployment MUST be in `Ready` state

#### Scenario: Operator waits for cert-manager

- GIVEN cert-manager is NOT installed
- WHEN the ArgoCD Application for the OTel Operator attempts to sync
- THEN the Operator webhook deployment MUST NOT become ready until cert-manager provides the TLS certificate

---

### Requirement: OTel Collector DaemonSet

The system MUST deploy an OpenTelemetry Collector as a DaemonSet (one pod per node) via the `OpenTelemetryCollector` CRD in sync wave 2. The DaemonSet agent SHALL collect host metrics, kubelet stats, and container logs, and forward all telemetry via OTLP to the Gateway.

#### Scenario: DaemonSet pods running on all nodes

- GIVEN the OTel Operator is ready (wave 1 complete)
- WHEN the `OpenTelemetryCollector` CR for the DaemonSet is applied (wave 2)
- THEN one Collector pod MUST be running on each k3s node
- AND each pod MUST be in `Ready` state

#### Scenario: Host metrics collection

- GIVEN the DaemonSet is running
- WHEN 30 seconds elapse
- THEN the `hostmetrics` receiver MUST collect CPU, memory, disk, and network metrics from the node
- AND forward them via OTLP to the OTel Gateway

#### Scenario: Kubelet stats collection

- GIVEN the DaemonSet is running
- WHEN the kubeletstats receiver scrapes the kubelet API
- THEN pod and container resource metrics (CPU, memory) MUST be collected
- AND forwarded via OTLP to the OTel Gateway

#### Scenario: Container log collection

- GIVEN the DaemonSet is running and containers are producing logs
- WHEN the `filelog` receiver reads from `/var/log/pods`
- THEN container log lines MUST be collected with metadata (namespace, pod, container)
- AND forwarded via OTLP to the OTel Gateway

---

### Requirement: OTel Collector Gateway

The system MUST deploy an OpenTelemetry Collector as a StatefulSet (1 replica) with TargetAllocator enabled via the `OpenTelemetryCollector` CRD in sync wave 3. The Gateway SHALL receive OTLP telemetry from the DaemonSet, enrich it with Kubernetes metadata, and route metrics to Prometheus and logs to Fluentbit.

#### Scenario: Gateway receiving data from DaemonSet

- GIVEN the DaemonSet is sending OTLP data
- WHEN the Gateway StatefulSet is running
- THEN the Gateway MUST accept OTLP on gRPC port 4317 and HTTP port 4318
- AND the `otlp` receiver MUST be configured for both protocols

#### Scenario: Kubernetes metadata enrichment

- GIVEN the Gateway receives telemetry from the DaemonSet
- WHEN the `k8sattributes` processor processes the telemetry
- THEN each log and metric MUST be enriched with Kubernetes metadata labels (namespace, pod, container, node, deployment, etc.)

#### Scenario: Metrics export to Prometheus

- GIVEN the Gateway is receiving metrics from the DaemonSet and application pods
- WHEN the `prometheusremotewrite` exporter sends data
- THEN metrics MUST be pushed to Prometheus at the configured remote_write endpoint
- AND Prometheus MUST NOT scrape any targets directly (push-only model)

#### Scenario: Logs export to Fluentbit

- GIVEN the Gateway is receiving logs from the DaemonSet
- WHEN the `fluentforward` exporter sends data
- THEN log records MUST be forwarded to the Fluentbit Aggregator via the Fluent Forward protocol

#### Scenario: TargetAllocator for application pod metrics

- GIVEN an application pod has `prometheus.io/scrape: "true"` annotations
- WHEN the TargetAllocator discovers the pod
- THEN the Gateway's `prometheus` receiver MUST scrape the pod's metrics endpoint
- AND the scraped metrics MUST be exported via `prometheusremotewrite` to Prometheus

---

### Requirement: Fluentbit Aggregator

The system MUST deploy Fluentbit as a Deployment (1 replica) in sync wave 3. Fluentbit SHALL receive logs from the OTel Gateway via Fluent Forward protocol and write them as Parquet files to Garage (S3-compatible storage) with Hive-style partitioning.

#### Scenario: Receiving logs from Gateway

- GIVEN the OTel Gateway's `fluentforward` exporter is configured to send to Fluentbit
- WHEN the Fluentbit Aggregator is running
- THEN Fluentbit MUST accept log records on the Fluent Forward input

#### Scenario: Parquet file upload to Garage

- GIVEN Fluentbit is receiving log records
- WHEN the buffer reaches `Total_File_Size` of 5 MB OR `Upload_Timeout` of 5 minutes elapses
- THEN Fluentbit MUST upload a Parquet file to Garage at `s3://logs/raw/containers/year=%Y/month=%m/day=%d/hour=%H/$UUID.parquet`

#### Scenario: Parquet format support

- GIVEN the Fluentbit S3 output plugin is configured with `format parquet`
- WHEN log records are written
- THEN the output files MUST be in Apache Parquet columnar format
- AND the Fluentbit version MUST be v4.1.0 or later to support Parquet output

---

### Requirement: Garage S3 Storage

The system MUST provide an S3-compatible object storage service (Garage) running as an Alpine LXC container on Proxmox, provisioned via community-scripts. Garage SHALL store Parquet log files with Hive-style time partitioning.

#### Scenario: Garage LXC running

- GIVEN the Garage LXC is provisioned on Proxmox via community-scripts
- WHEN the LXC container is running
- THEN the Garage service MUST listen on port 3900 (S3 API), 3902 (Web UI), and 3903 (Admin API)
- AND the `logs` bucket MUST exist and be configured with S3 credentials

#### Scenario: Parquet file storage with Hive partitioning

- GIVEN Fluentbit uploads Parquet files to Garage
- WHEN files are stored
- THEN files MUST be located at `s3://logs/raw/containers/year={YYYY}/month={MM}/day={DD}/hour={HH}/{UUID}.parquet`
- AND the S3 API MUST be accessible from cluster nodes at the Garage LXC IP on port 3900

#### Scenario: Garage resource allocation

- GIVEN the Garage LXC is provisioned
- WHEN configured via community-scripts
- THEN the LXC MUST have 1 CPU, 512 MB RAM, and 5 GB disk allocated
- AND the OS MUST be Alpine Linux

---

### Requirement: Prometheus Metric Storage

The system MUST deploy Prometheus on a Proxmox VM or LXC to receive metrics via remote_write from the OTel Gateway. Prometheus SHALL operate in push-only mode with no scrape targets.

#### Scenario: Prometheus receiving remote_write

- GIVEN the OTel Gateway's `prometheusremotewrite` exporter is configured
- WHEN Prometheus is running on port 9090
- THEN Prometheus MUST accept remote_write requests and store time-series data in its local TSDB

#### Scenario: No scrape configuration

- GIVEN Prometheus is configured for this monitoring architecture
- WHEN inspecting the Prometheus configuration
- THEN Prometheus MUST NOT have any `scrape_configs` defined
- AND all metrics MUST arrive via `remote_write` receiver only

---

### Requirement: Grafana Visualization

The system MUST deploy Grafana on a Proxmox VM or LXC with two datasources: Prometheus for metrics and DuckDB (with S3 plugin) for Parquet log queries from Garage.

#### Scenario: Prometheus datasource

- GIVEN Grafana is running on port 3000
- WHEN the Prometheus datasource is configured
- THEN Grafana MUST be able to query Prometheus for metric dashboards

#### Scenario: DuckDB log datasource

- GIVEN Grafana is running
- WHEN the DuckDB datasource is configured with the S3 plugin pointing to Garage
- THEN Grafana MUST be able to query Parquet log files using DuckDB SQL with macros (e.g., `logs()`)
- AND the DuckDB plugin MUST be installed in Grafana

---

### Requirement: ArgoCD App of Apps Integration

All in-cluster monitoring components MUST be deployed as ArgoCD Application definitions under the `apps/` directory, following the existing App of Apps pattern. ArgoCD sync waves MUST enforce the correct deployment order.

#### Scenario: ArgoCD Applications defined

- GIVEN the monitoring architecture is being deployed
- WHEN ArgoCD discovers Application definitions in `apps/`
- THEN the following Applications MUST exist: `otel-operator` (wave 1), `otel-collector` (wave 2), `fluentbit` (wave 3)
- AND each Application MUST use `automated` sync policy with `prune: true` and `selfHeal: true`

#### Scenario: Sync wave ordering

- GIVEN all ArgoCD Applications are defined
- WHEN ArgoCD triggers a sync
- THEN the OTel Operator (wave 1) MUST sync before OTel Collectors (wave 2)
- AND OTel Collectors (wave 2) MUST sync before Fluentbit (wave 3)

---

### Requirement: Network Connectivity Between Tiers

The system MUST ensure network connectivity between the k3s cluster and Proxmox LXC/VM components for metrics, logs, and storage traffic.

#### Scenario: Cluster to Garage S3

- GIVEN the k3s cluster nodes and the Garage LXC are on the same LAN (192.168.4.x)
- WHEN Fluentbit in the cluster writes to Garage
- THEN the connection to port 3900 MUST succeed without firewall or routing issues

#### Scenario: Cluster to Prometheus remote_write

- GIVEN the k3s cluster nodes and the Prometheus VM/LXC are on the same LAN
- WHEN the OTel Gateway pushes metrics via remote_write
- THEN the connection to port 9090 MUST succeed

---

### Requirement: Secrets and Credentials Management

The system MUST configure S3 credentials for Fluentbit to authenticate with Garage. Credentials SHALL be extracted from the Garage LXC (`~/garage.creds`) and provided to Fluentbit via Kubernetes Secret or ConfigMap.

#### Scenario: Garage S3 credentials configured in Fluentbit

- GIVEN Garage has been provisioned with S3 access key and secret key in `~/garage.creds`
- WHEN the Fluentbit Aggregator is deployed
- THEN the S3 access key and secret key MUST be referenced in the Fluentbit S3 output configuration
- AND credentials MUST be stored in a Kubernetes Secret (not plaintext in ConfigMap)

#### Scenario: Prometheus remote_write auth

- GIVEN the OTel Gateway sends metrics to Prometheus
- WHEN configuring the `prometheusremotewrite` exporter endpoint
- THEN basic auth or mTLS MAY be configured, or auth MAY be omitted for homelab simplicity
- AND the decision MUST be documented in the configuration

---

## Component Specifications

### OpenTelemetry Operator

| Property | Value |
|----------|-------|
| Image | `ghcr.io/open-telemetry/opentelemetry-operator/opentelemetry-operator:0.150.0` |
| Namespace | `opentelemetry-operator-system` |
| Deployment kind | Upstream manifest (Deployment + CRDs + webhook) |
| Sync wave | 1 |
| Dependencies | cert-manager |
| Managed by | ArgoCD Application |

### OTel Collector DaemonSet

| Property | Value |
|----------|-------|
| Image | `otel/opentelemetry-collector-contrib:0.123.0` |
| CRD | `OpenTelemetryCollector` (agent mode) |
| Kind | DaemonSet (one pod per node) |
| Sync wave | 2 |
| CPU request | 100m |
| Memory request | 128Mi |
| Memory limit | 512Mi |
| Receivers | `hostmetrics` (CPU, memory, disk, network; interval 30s), `kubeletstats` (pod/container metrics), `filelog` (/var/log/pods) |
| Processors | `k8sattributes`, `batch`, `memory_limiter`, `resource/cluster` |
| Exporters | `otlp` (to Gateway, gRPC) |
| Managed by | ArgoCD Application |

### OTel Collector Gateway

| Property | Value |
|----------|-------|
| Image | `otel/opentelemetry-collector-contrib:0.123.0` |
| CRD | `OpenTelemetryCollector` (deployment mode with TargetAllocator) |
| Kind | StatefulSet, 1 replica |
| Sync wave | 3 |
| CPU request | 250m |
| Memory request | 256Mi |
| Memory limit | 1Gi |
| Receivers | `otlp` (gRPC 4317, HTTP 4318), `prometheus` (via TargetAllocator) |
| Processors | `k8sattributes`, `batch`, `memory_limiter`, `resource/cluster` |
| Exporters | `fluentforward` (to Fluentbit), `prometheusremotewrite` (to Prometheus) |
| Metrics port | 8888 (self-metrics) |
| TargetAllocator | Enabled (discovers pods with `prometheus.io/scrape` annotations) |
| Managed by | ArgoCD Application |

### Fluentbit Aggregator

| Property | Value |
|----------|-------|
| Image | `fluent/fluent-bit:4.1` (minimum v4.1.0 for Parquet support) |
| Kind | Deployment, 1 replica |
| Sync wave | 3 |
| Input | Fluent Forward (from OTel Gateway) |
| Output | S3 (Garage, `format parquet`) |
| Buffer config | `Total_File_Size 5M`, `Upload_Timeout 5m` |
| S3 endpoint | `http://<GARAGE_LXC_IP>:3900` |
| S3 bucket | `logs` |
| S3 key format | `raw/containers/year=%Y/month=%m/day=%d/hour=%H/$UUID.parquet` |
| Managed by | ArgoCD Application |

### Garage

| Property | Value |
|----------|-------|
| Platform | Proxmox LXC (Alpine Linux) |
| Provisioning | community-scripts (`garage.sh`) |
| CPU | 1 vCPU |
| RAM | 512 MB |
| Disk | 5 GB |
| S3 API port | 3900 |
| Admin API port | 3903 |
| Web UI port | 3902 |
| Config path | `/etc/garage.toml` |
| Credentials | `~/garage.creds` |
| Managed by | Manual / Proxmox |

### Prometheus

| Property | Value |
|----------|-------|
| Platform | Proxmox VM or LXC |
| Version | Latest stable |
| Port | 9090 |
| Input | `remote_write` receiver (push-only from OTel Gateway) |
| Scrape targets | None (OTel always pushes) |
| Storage | Local TSDB |
| Managed by | Manual / Proxmox |

### Grafana

| Property | Value |
|----------|-------|
| Platform | Proxmox VM or LXC |
| Version | Latest stable |
| Port | 3000 |
| Datasource 1 | Prometheus (at `http://<PROMETHEUS_IP>:9090`) |
| Datasource 2 | DuckDB with S3 plugin (reading Parquet from Garage at `http://<GARAGE_LXC_IP>:3900`) |
| Plugins | DuckDB Grafana plugin |
| Managed by | Manual / Proxmox |

---

## Network & Ports

| Component | Port | Protocol | Direction | Purpose |
|-----------|------|----------|-----------|---------|
| OTel Gateway | 4317 | gRPC | Inbound (from DaemonSet, apps) | OTLP receiver |
| OTel Gateway | 4318 | HTTP | Inbound (from DaemonSet, apps) | OTLP receiver |
| OTel Gateway | 8888 | HTTP | Inbound | Self-metrics |
| Fluentbit | 24224 | TCP | Inbound (from Gateway) | Fluent Forward input |
| Garage | 3900 | HTTP | Inbound (from cluster) | S3 API |
| Garage | 3902 | HTTP | Inbound (admin) | Web UI |
| Garage | 3903 | HTTP | Inbound (admin) | Admin API |
| Prometheus | 9090 | HTTP | Inbound (from Gateway) | remote_write receiver |
| Grafana | 3000 | HTTP | Inbound (user) | Web UI |

All cluster-to-Proxmox traffic traverses the LAN (192.168.4.x). No overlay or VPN is required.

---

## S3 Bucket Structure (Garage)

### Bucket: `logs`

**Raw (write path — Fluentbit):**
```
s3://logs/raw/containers/year=2026/month=06/day=05/hour=14/{UUID}.parquet
s3://logs/raw/containers/year=2026/month=06/day=05/hour=14/{UUID}.parquet
s3://logs/raw/containers/year=2026/month=06/day=05/hour=15/{UUID}.parquet
```

**Compacted (optional future step — batch job or DuckDB CTAS):**
```
s3://logs/hourly/containers/year=2026/month=06/day=05/hour=14/data.parquet
```

Hive-style partitioning enables DuckDB to prune irrelevant time ranges during queries using `year`, `month`, `day`, `hour` partition columns.

---

## OTel Config Structure

### DaemonSet — Receivers

```yaml
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
    endpoint: "${K8S_NODE_NAME}:10250"
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
```

### DaemonSet — Processors

```yaml
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
```

### DaemonSet — Exporters

```yaml
exporters:
  otlp:
    endpoint: "otel-gateway-collector:4317"
    tls:
      insecure: true
```

### Gateway — Receivers

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
  prometheus:
    config:
      scrape_configs:
        - job_name: 'otel-gateway'
          scrape_interval: 30s
          # TargetAllocator injects dynamic targets here
```

### Gateway — Processors

```yaml
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
```

### Gateway — Exporters

```yaml
exporters:
  fluentforward:
    endpoint: "fluentbit-aggregator:24224"
  prometheusremotewrite:
    endpoint: "http://<PROMETHEUS_IP>:9090/api/v1/write"
    # tls/insecure/auth to be confirmed at design time
```

### Service Pipelines

```yaml
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
```

---

## Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC-1 | OTel Operator installed, CRD available | `kubectl get crd opentelemetrycollectors.opentelemetry.io` succeeds |
| AC-2 | DaemonSet pods running on all nodes | `kubectl get pods -l app.kubernetes.io/name=otel-collector-daemonset` shows 3 running pods (one per node) |
| AC-3 | Gateway StatefulSet running | `kubectl get statefulset -l app.kubernetes.io/name=otel-collector-gateway` shows 1/1 ready |
| AC-4 | Fluentbit Aggregator running | `kubectl get deployment fluentbit-aggregator` shows 1/1 ready |
| AC-5 | All in-cluster components ArgoCD synced | `argocd app list` shows all monitoring apps as `Synced` and `Healthy` |
| AC-6 | Metrics arriving in Prometheus | Query Prometheus UI: `up` or any host metric time-series has recent data points |
| AC-7 | Logs arriving in Garage | List objects in the `logs` bucket via Garage Admin API (port 3903) or S3 CLI; Parquet files exist under `raw/containers/` with correct Hive partitions |
| AC-8 | Grafana queries Prometheus | Create a Grafana panel with PromQL (e.g., `system_cpu_usage`); data renders |
| AC-9 | Grafana queries Parquet logs | Create a Grafana panel with DuckDB SQL against the `logs` bucket; log records are returned |
| AC-10 | Sync wave ordering enforced | ArgoCD syncs Operator before Collectors, Collectors before Fluentbit |
| AC-11 | Fluentbit writes Parquet format | Download a file from Garage; verify it is valid Parquet using `parquet-tools` or DuckDB |
| AC-12 | Network connectivity cluster → Garage | From a pod in the cluster: `curl http://<GARAGE_IP>:3900` returns Garage response |
| AC-13 | Network connectivity cluster → Prometheus | From a pod in the cluster: `curl http://<PROMETHEUS_IP>:9090/-/healthy` returns 200 |
| AC-14 | Kubernetes metadata enrichment | Log records in Parquet contain `k8s_namespace_name`, `k8s_pod_name`, `k8s_container_name` fields |
| AC-15 | No Prometheus scrape targets | Prometheus config has no `scrape_configs` or all scrape jobs are disabled; all data arrives via remote_write |

---

## Risks / Constraints

| # | Risk | Impact | Likelihood | Mitigation |
|---|------|--------|------------|------------|
| R1 | **Fluentbit S3 Parquet support requires v4.1+** | Fluentbit cannot write Parquet files if version is too old | Medium | Pin Fluentbit image to `fluent/fluent-bit:4.1` or later. Verify Parquet support in release notes before deploying. |
| R2 | **Garage credentials extraction** | Fluentbit cannot authenticate with Garage S3 if credentials are not configured | Low | Extract `access_key_id` and `secret_access_key` from `~/garage.creds` on the Garage LXC. Create a Kubernetes Secret with these values. Document the process. |
| R3 | **Network between cluster nodes and Proxmox LXCs/VMs** | Telemetry pipeline fails if ports are blocked | Low | All components are on the same LAN (192.168.4.x). Verify firewall rules on Proxmox allow ports 3900 (Garage S3) and 9090 (Prometheus remote_write). |
| R4 | **cert-manager required for OTel Operator webhook** | Operator fails to start without cert-manager | Medium | cert-manager MUST be installed before the OTel Operator sync wave. If cert-manager is not already deployed, it becomes a prerequisite dependency. |
| R5 | **DaemonSet memory pressure on small nodes** | OOMKilled pods if memory limit too low | Low | Set memory request to 128Mi and limit to 512Mi. Monitor `container_memory_working_set_bytes` during initial rollout. Tune `memory_limiter` processor if needed. |
| R6 | **Garage single point of failure** | Log loss if Garage LXC goes down | Low | Garage runs outside the cluster on Proxmox. Survives cluster resets. Can replicate to a second Garage instance in a future change. |
| R7 | **Fluentbit to Garage latency on LAN** | Log delivery delay if network is slow | Low | LAN latency is sub-millisecond. Fluentbit buffers locally before upload (5 MB / 5 min). Acceptable for homelab scale. |
| R8 | **OTel Operator CRDs may conflict** | Existing CRDs from a prior install could conflict | Low | Ensure clean state before applying. ArgoCD server-side apply resolves most conflicts. |

---

## Non-Goals

- **Distributed tracing** — OTLP endpoints support traces, but trace collection/storage/querying is not scoped.
- **Kubernetes events collection** — `k8sobjects` receiver is not included.
- **Alerting** — No Alertmanager or alert rules. Can be added as a follow-up.
- **Grafana dashboard provisioning** — Dashboards created manually. Automated provisioning via grafana-operator or ConfigMaps is a future enhancement.
- **Log-based alerting** — DuckDB query-based alerts are out of scope.
- **Garage replication** — Single-node Garage. Multi-node replication is a future change.
