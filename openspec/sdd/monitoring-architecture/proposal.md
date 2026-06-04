# Proposal: monitoring-architecture

**Change:** monitoring-architecture  
**Phase:** proposal  
**Stacks affected:** `infrastructure`, `apps`, `bootstrap`  
**Status:** draft

---

## 1. Motivation

The homelab currently has **no monitoring, logging, or observability stack**. There is no visibility into:

- Cluster resource utilization (CPU, memory, disk, network per node and pod)
- Application logs (only available via `kubectl logs` with no persistence or search)
- Service health, errors, or performance trends
- Historical data for capacity planning or troubleshooting

Without observability, diagnosing issues is reactive and manual. Every troubleshooting session requires SSH-ing into nodes, running `kubectl` commands, and piecing together fragmented information. A unified observability stack provides a single pane of glass for cluster and application health.

---

## 2. Proposed Architecture

The monitoring architecture follows a three-tier model:

- **In-cluster (k3s, managed via ArgoCD):** Collection and aggregation layer using OpenTelemetry, with log offload to Fluentbit for Parquet conversion.
- **Proxmox LXC (Garage):** S3-compatible object storage running as an LXC container on Proxmox. Stores Parquet log files. Configured via community-scripts (Alpine Linux, 512 MB RAM, 5 GB disk).
- **Proxmox VM/LXC (Prometheus + Grafana):** Long-term metric storage (Prometheus, receives remote_write from OTel). Visualization (Grafana with DuckDB + Prometheus datasources).

### Design principles

- **Cost-efficient:** Avoid heavy stacks like Elasticsearch/Loki. Use Parquet + DuckDB for log storage and querying, which is ~10× cheaper for homelab-scale data.
- **Unified collection:** OpenTelemetry as the single collection protocol — DaemonSet for node-level metrics and logs, Gateway for aggregation.
- **Separation of concerns:** Metrics and logs follow different paths to optimize for their access patterns.
- **GitOps-managed:** All in-cluster components deployed via ArgoCD Applications under the existing app-of-apps pattern.

---

## 3. Components

### 3.1 In-cluster (k3s via ArgoCD)

| # | Component | Kind | Sync Wave | Description |
|---|-----------|------|-----------|-------------|
| 1 | **OpenTelemetry Operator** | Upstream manifests | 1 | Official OTel Operator (CRDs + Deployment + cert-manager webhook). Provides the `OpenTelemetryCollector` CRD. Depends on cert-manager. |
| 2 | **OTel Collector DaemonSet** | DaemonSet | 2 | Per-node agent. Collects: `hostmetrics` (CPU, mem, disk, net every 30s), `kubeletstats` (pod/container resources), `filelog` (container logs from `/var/log/pods`). Sends all data via OTLP to the Gateway. |
| 3 | **OTel Collector Gateway** | StatefulSet + TargetAllocator | 3 | Aggregation layer (1 replica). Receives OTLP from DaemonSet and applications. `k8sattributes` processor enriches data. Routes logs to Fluentbit via Fluent Forward protocol. Pushes metrics to Prometheus via `prometheusremotewrite` exporter. |
| 4 | **Fluentbit Aggregator** | Deployment | 3 | Receives logs from OTel Gateway via Fluent Forward. Writes Parquet files to Garage (S3) with Hive-style partitioning (`year=/month=/day=/hour=`). Buffering: `Total_File_Size=5M`, `Upload_Timeout=5m`. |

### 3.2 External VM (Proxmox)

| # | Component | Location | Description |
|---|-----------|----------|-------------|
| 5 | **Garage** | Proxmox LXC | Lightweight S3-compatible object storage (Rust). Runs as Alpine LXC via community-scripts. Ports: 3900 (S3 API), 3902 (Web UI), 3903 (Admin API). 512 MB RAM, 5 GB disk. Stores Parquet log files with Hive-style partitioning. |
| 6 | **Prometheus** | Proxmox VM/LXC | Receives metrics via `prometheusremotewrite` from OTel Gateway. Long-term metric storage with local TSDB. Does NOT scrape — OTel always pushes. |
| 7 | **Grafana** | Proxmox VM/LXC | Visualization layer. Three datasources: (1) Prometheus for metric dashboards, (2) DuckDB with S3 plugin for Parquet log queries from Garage. Uses DuckDB macros (`logs()`) for log querying. |

---

## 4. Data Flow

### 4.1 Log Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│  k3s Cluster (in-cluster, ArgoCD-managed)                           │
│                                                                     │
│  ┌──────────────┐    filelog     ┌──────────────┐                   │
│  │ /var/log/pods │ ────────────► │ OTel DaemonSet│                   │
│  │ (containers)  │  (per node)   │ (otel-collector) │                │
│  └──────────────┘               └──────┬───────┘                   │
│                                        │ OTLP                       │
│                                        ▼                            │
│                               ┌──────────────────┐                  │
│                               │ OTel Gateway      │                  │
│                               │ (StatefulSet + TA)│                  │
│                               │ k8sattributes     │                  │
│                               └──────┬───────────┘                  │
│                                      │ fluentforward                 │
│                                      ▼                               │
│                               ┌──────────────────┐                  │
│                               │ Fluentbit          │                  │
│                               │ Aggregator         │                  │
│                               │ (Deployment)       │                  │
│                               └──────┬───────────┘                  │
│                                      │ Parquet (s3_out)             │
│                                      ▼                               │
└──────────────────────────────────────┼───────────────────────────────┘
                                       │
                              ┌────────┴──────────┐
                              │ Garage LXC          │
                              │ (Proxmox, Alpine)   │
                              │ Port 3900 (S3 API)   │
                              │ /logs/year=/month=… │
                              └────────┬───────────┘
                                       │
                              DuckDB + S3 API (read)
                                       ▼
                              ┌──────────────────┐
                              │ Grafana (VM)      │
                              │ DuckDB datasource │
                              │ logs() macro      │
                              └──────────────────┘
```

### 4.2 Metrics Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│  k3s Cluster (in-cluster, ArgoCD-managed)                           │
│                                                                     │
│  ┌──────────────┐  hostmetrics  ┌──────────────┐                   │
│  │ Node OS       │ ────────────► │ OTel DaemonSet│                   │
│  │ (cpu,mem,disk)│  (every 30s)  │ (otel-collector) │                │
│  └──────────────┘               └──────┬───────┘                   │
│                                        │                            │
│  ┌──────────────┐  kubeletstats       │                            │
│  │ Kubelet API   │ ───────────────────►│                            │
│  │ (pod metrics) │                     │ OTLP                       │
│  └──────────────┘                     ▼                            │
│                               ┌──────────────────┐                  │
│                               │ OTel Gateway      │                  │
│                               │ (StatefulSet + TA)│                  │
│                               │ prometheus receiver│                 │
│                               │ (scrapes pods via TA)│                │
│                               └──────┬───────────┘                  │
│                                      │ prometheusremotewrite         │
│                                      │ (push, not scrape)            │
└──────────────────────────────────────┼───────────────────────────────┘
                                       │
                              ┌────────┴──────────┐
                              │ Prometheus (VM)    │
                              │ remote_write       │
                              │ receives push      │
                              │ from OTel Gateway  │
                              └────────┬──────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ Grafana (VM)      │
                              │ Prometheus ds     │
                              └──────────────────┘
```

### 4.3 Application Pod Metrics (annotation-driven)

```
┌──────────────────────────────────────┐
│  App Pod with prometheus annotations │
│  prometheus.io/scrape: "true"        │
│  prometheus.io/port: "8080"          │
│  prometheus.io/path: "/metrics"      │
└──────────────┬───────────────────────┘
               │ TargetAllocator discovers
               ▼
┌──────────────────────────┐
│ OTel Gateway              │
│ TargetAllocator           │
│ (dynamic scrape targets)  │
└──────────────┬───────────┘
               │ prometheusremotewrite (push)
               ▼
┌──────────────────────────┐
│ Prometheus (VM)           │
│ remote_write receiver     │
└──────────────────────────┘
```

---

## 5. Alternatives Considered

### 5.1 Logging: Full Loki stack vs. Fluentbit + S3 Parquet + DuckDB

| Aspect | Loki + Promtail | Fluentbit + Garage + DuckDB (proposed) |
|--------|----------------|----------------------------------------|
| Storage | Indexed, on-disk or S3 (chunks) | Parquet files on S3 (Garage) |
| Query | LogQL | DuckDB SQL + macros |
| Resource usage | Moderate-High (indexing overhead) | Low (write-only aggregation, read on demand) |
| Retention cost | Higher (indexes) | Lower (columnar compression) |
| Complexity | Requires Loki + index gateway | Simpler: Fluentbit → S3 → query |

**Decision:** Fluentbit + Garage (LXC) + DuckDB. Better suited for homelab scale. Log queries are less frequent than metric queries, so read-on-demand via DuckDB is acceptable. Garage runs as a lightweight Alpine LXC on Proxmox, separate from the cluster.

### 5.2 Metrics: Prometheus Operator vs. bare Prometheus + OTel Gateway

| Aspect | Prometheus Operator | Prometheus (VM) + OTel Gateway (proposed) |
|--------|-------------------|------------------------------------------|
| Deployment | CRD-heavy, runs in-cluster | External VM, simpler lifecycle |
| Scraping | ServiceMonitor/PodMonitor | OTel TargetAllocator + manual scrape config |
| Storage | In-cluster PVC or remote-write | Local TSDB on VM |
| Maintenance | More components to manage | Fewer moving parts in-cluster |

**Decision:** External Prometheus + OTel Gateway with `prometheusremotewrite`. OTel always pushes metrics, Prometheus never scrapes. Keeps the cluster lighter, separates metric retention from cluster lifecycle (metrics survive cluster rebuilds), and aligns with the homelab's existing VM/LXC infrastructure pattern.

### 5.3 Log collection: Fluentbit DaemonSet vs. OTel DaemonSet + filelog

| Aspect | Fluentbit DaemonSet (direct to S3) | OTel DaemonSet → Gateway → Fluentbit → S3 (proposed) |
|--------|------------------------------------|------------------------------------------------------|
| Protocol | Proprietary/fluentd | OTLP (standard) |
| Enrichment | Limited k8s metadata | Full k8sattributes processor in Gateway |
| Unified pipeline | No (separate from metrics) | Yes (metrics + logs via OTel) |
| Complexity | Lower (one hop) | Higher (more hops) |

**Decision:** OTel DaemonSet + Gateway + Fluentbit. The extra hop is worth it for unified collection, k8s metadata enrichment, and the ability to route different telemetry types differently.

---

## 6. Affected Areas

### Infrastructure manifests (`infrastructure/base/`)

Currently contains only a namespace. Will grow to include:
- OpenTelemetry Operator manifests
- OTel Collector DaemonSet and Gateway resources
- Fluentbit Aggregator deployment + config

### ArgoCD Applications (`apps/`)

New Application definitions for in-cluster monitoring components:
- `otel-operator.yaml` — OpenTelemetry Operator
- `otel-collector.yaml` — OTel DaemonSet + Gateway
- `fluentbit.yaml` — Fluentbit Aggregator

### Bootstrap (`bootstrap/argocd/`)

No changes to app-of-apps beyond adding references to new Application definitions in `apps/`.

### Proxmox (manual setup or Ansible)

Garage is installed as an LXC container via community-scripts:
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/garage.sh)"
```
- Alpine Linux, 1 CPU, 512 MB RAM, 5 GB disk
- Ports: 3900 (S3 API), 3902 (Web UI), 3903 (Admin API)
- Config: `/etc/garage.toml`
- Credentials: `~/garage.creds`

Prometheus and Grafana on a Proxmox VM or LXC (to be determined).

---

## 7. Risks and Mitigations

| # | Risk | Impact | Likelihood | Mitigation |
|---|------|--------|------------|------------|
| R1 | **Garage single point of failure** | Log loss if Garage LXC goes down | Low | Garage runs as an LXC on Proxmox, not in-cluster. Survives cluster resets. Can add Garage replication across multiple LXCs later. |
| R2 | **LXC privilege constraints** | OTel DaemonSet may not access `/var/log/pods` or host metrics | Medium | k3s runs in privileged LXC containers (per cluster conventions). Verify `filelog` and `hostmetrics` receivers have correct permissions. |
| R3 | **Resource overhead on 3-node cluster** | Monitoring components consume cluster resources | Low | DaemonSet is lightweight (3 pods). Gateway is 1 replica. Fluentbit is 1 replica. Total estimated: ~300-500mCPU, ~500Mi memory across the cluster. |
| R4 | **Parquet log query latency** | DuckDB queries on S3 files may be slow | Low | Hive-style partitioning by time narrows scans. Homelab log volume is low (~GBs/day at most). Acceptable for ad-hoc queries. |
| R5 | **OTel Operator CRD dependency** | Operator must be ready before Collector CRs are applied | Medium | Use ArgoCD sync-waves: Operator (wave 1), DaemonSet/Gateway (wave 2+), Fluentbit (wave 3). |
| R6 | **Network between cluster and Proxmox LXCs/VMs** | OTel must push metrics and logs to Prometheus (remote_write) and Garage (S3) on Proxmox | Low | All on same LAN (192.168.4.x). Reliable internal network. OTel has retry/buffering for transient failures. |
| R7 | **Fluentbit S3 output buffering** | Data loss if Fluentbit crashes before upload | Low | Buffering to local disk before upload. 5M file size / 5m timeout ensures regular flushes. |

---

## 8. Non-Goals

The following are **explicitly out of scope** for this change:

- **Tracing:** OTel Gateway exposes OTLP endpoints that support traces, but trace collection, storage, and visualization are not scoped. Can be added later.
- **Kubernetes events:** The `k8sobjects` receiver for Kubernetes events is not included. Can be added later as an additional receiver on the Gateway.
- **Alertmanager:** No alerting rules or Alertmanager deployment. Prometheus alerting can be configured as a follow-up change.
- **Grafana dashboard provisioning:** Dashboards will be created manually initially. Automated dashboard provisioning (via ConfigMaps or grafana-operator) can be added later.
- **Log-based alerting:** DuckDB query-based log alerts are out of scope.

---

## 9. Success Criteria

1. **Garage LXC** is running on Proxmox and accessible via S3 API port 3900 from the cluster.
2. **OTel Operator** is installed and the `OpenTelemetryCollector` CRD is available.
3. **OTel Collector DaemonSet** is running on all 3 nodes, reporting hostmetrics and kubeletstats.
4. **OTel Collector Gateway** is running, receiving data from DaemonSet, and pushing metrics to Prometheus via `prometheusremotewrite`.
5. **Fluentbit Aggregator** is writing Parquet files to Garage with Hive-style partitioning.
6. **Prometheus** (VM/LXC) is receiving remote_write from OTel Gateway and storing time-series data.
7. **Grafana** (VM/LXC) can query both Prometheus metrics and Parquet logs from Garage via DuckDB.
8. All in-cluster components are managed via ArgoCD (synced, self-healing).
