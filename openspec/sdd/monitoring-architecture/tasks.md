# Tasks: monitoring-architecture

**Change:** monitoring-architecture
**Phase:** tasks
**Stacks affected:** `infrastructure`, `apps`
**Status:** draft

---

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~350 |
| 400-line budget risk | Medium |
| Chained PRs recommended | No |
| Suggested split | single PR |
| Delivery strategy | single-pr |
| Chain strategy | pending |

```text
Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Medium
```

### Forecast Rationale

- 14 new YAML files across `infrastructure/monitoring/` and `apps/`
- ~350 lines of reviewable YAML (all in-cluster manifests)
- 4 manual phases with zero repo changes (Garage, Prometheus, Grafana, verification)
- Fits within the 400-line budget if YAML is kept concise
- Single PR is sufficient — all in-cluster manifests form one atomic unit (ArgoCD waves enforce ordering)

---

## Phase 1: Proxmox Infrastructure (Manual)

> All tasks in this phase are performed on the Proxmox host / Garage LXC. No repo changes.

### T1.1 — Provision Garage LXC

| Field | Value |
|-------|-------|
| **id** | T1.1 |
| **title** | Provision Garage LXC on Proxmox |
| **description** | Run the community-scripts Garage installer on the Proxmox host. Creates an Alpine Linux LXC (1 CPU, 512 MB RAM, 5 GB disk). Garage listens on ports 3900 (S3), 3902 (Web UI), 3903 (Admin API). |
| **files** | _(none — Proxmox host)_ |
| **phase** | 1 |
| **type** | provision |
| **depends_on** | _none_ |
| **review_estimate** | 0 |
| **manual** | true |

**How:** On Proxmox host:
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/garage.sh)"
```

**Verify:** LXC running, `garage` service active, ports 3900/3902/3903 listening.

---

### T1.2 — Create S3 bucket and API key

| Field | Value |
|-------|-------|
| **id** | T1.2 |
| **title** | Create logs bucket and S3 credentials |
| **description** | SSH into the Garage LXC. Create the `logs` bucket. Create an S3 API key named `monitoring`. Grant read/write access. Record the access key and secret key from `garage key info monitoring`. |
| **files** | _(none — Garage LXC)_ |
| **phase** | 1 |
| **type** | configure |
| **depends_on** | T1.1 |
| **review_estimate** | 0 |
| **manual** | true |

**How:** On the Garage LXC:
```bash
garage bucket create logs
garage key create monitoring
garage bucket allow --key monitoring --read --write logs
garage key info monitoring
```

**Verify:** `garage bucket list` shows `logs`. `garage key info monitoring` returns credentials.

---

### T1.3 — Record Garage network info

| Field | Value |
|-------|-------|
| **id** | T1.3 |
| **title** | Record Garage LXC IP and credentials |
| **description** | Note the Garage LXC IP address (`ip -4 addr`), the S3 access key, and the secret key. Store securely (not in the repo). These values are needed for: Fluentbit `GARAGE_S3_ENDPOINT` (T4.5), Grafana DuckDB datasource (T6.4). |
| **files** | _(none — documentation)_ |
| **phase** | 1 |
| **type** | configure |
| **depends_on** | T1.2 |
| **review_estimate** | 0 |
| **manual** | true |

**Verify:** Three values recorded: `<GARAGE_IP>`, `<GARAGE_ACCESS_KEY>`, `<GARAGE_SECRET_KEY>`.

---

## Phase 2: Monitoring Namespace + OTel Operator (Wave 1)

### T2.1 — Create monitoring namespace manifest

| Field | Value |
|-------|-------|
| **id** | T2.1 |
| **title** | Create monitoring namespace |
| **description** | Create the `monitoring` namespace manifest. This namespace will host the OTel Collector DaemonSet, Gateway, and Fluentbit. The OTel Operator deploys to its own `opentelemetry-operator-system` namespace (created by the operator manifest). |
| **files** | `infrastructure/monitoring/namespace.yaml` |
| **phase** | 2 |
| **type** | create |
| **depends_on** | _none_ |
| **review_estimate** | 5 |
| **manual** | false |

**Content:** Namespace `monitoring` (plain YAML, matches existing `infrastructure/base/namespace.yaml` pattern).

---

### T2.2 — Create OTel Operator ArgoCD Application

| Field | Value |
|-------|-------|
| **id** | T2.2 |
| **title** | Create OTel Operator Application |
| **description** | Create an ArgoCD Application manifest pointing to the upstream OTel Operator repo (`open-telemetry/opentelemetry-operator`, tag `v0.122.0`, path `config/default`). Uses ArgoCD's native kustomize rendering. Sync wave 1. Deploys to `opentelemetry-operator-system` namespace. |
| **files** | `apps/otel-operator.yaml` |
| **phase** | 2 |
| **type** | create |
| **depends_on** | _none_ |
| **review_estimate** | 25 |
| **manual** | false |

**Verify:** After ArgoCD sync: `kubectl get crd opentelemetrycollectors.opentelemetry.io` succeeds. `kubectl get pods -n opentelemetry-operator-system` shows operator running.

**Prerequisite:** cert-manager MUST already be installed in the cluster.

---

## Phase 3: OTel Collector DaemonSet (Wave 2)

### T3.1 — Create DaemonSet RBAC

| Field | Value |
|-------|-------|
| **id** | T3.1 |
| **title** | Create DaemonSet ServiceAccount and RBAC |
| **description** | Create a ServiceAccount, ClusterRole (get/list/watch on pods, namespaces, nodes, deployments, replicasets, statefulsets, daemonsets, jobs, cronjobs), and ClusterRoleBinding for the OTel Collector DaemonSet. Required for `k8sattributes`, `kubeletstats`, and `filelog` receivers. |
| **files** | `infrastructure/monitoring/otel-collector/rbac.yaml` |
| **phase** | 3 |
| **type** | create |
| **depends_on** | T2.1 |
| **review_estimate** | 40 |
| **manual** | false |

---

### T3.2 — Create DaemonSet OpenTelemetryCollector CR

| Field | Value |
|-------|-------|
| **id** | T3.2 |
| **title** | Create OTel Collector DaemonSet CR |
| **description** | Create the `OpenTelemetryCollector` CR in `mode: daemonset`. Image: `otel/opentelemetry-collector-contrib:0.123.0`. Receivers: `hostmetrics` (cpu, memory, disk, network; 30s), `kubeletstats` (30s, insecure_skip_verify), `filelog` (/var/log/pods with container parser). Processors: `memory_limiter` (400 MiB), `batch`, `k8sattributes`, `resource/cluster`. Exporter: `otlp` to `otel-gateway-collector:4317` (insecure). Two pipelines: metrics (hostmetrics+kubeletstats→otlp) and logs (filelog→otlp). HostPath volumes for `/var/log/pods` and `/var/log`. |
| **files** | `infrastructure/monitoring/otel-collector/collector.yaml` |
| **phase** | 3 |
| **type** | create |
| **depends_on** | T2.2, T3.1 |
| **review_estimate** | 90 |
| **manual** | false |

**Verify:** After ArgoCD sync: `kubectl get pods -n monitoring -l app.kubernetes.io/name=collector-daemonset-collector` shows 3 pods (one per node), all Running.

---

### T3.3 — Create DaemonSet ArgoCD Application

| Field | Value |
|-------|-------|
| **id** | T3.3 |
| **title** | Create OTel Collector Application |
| **description** | Create an ArgoCD Application manifest pointing to `infrastructure/monitoring/otel-collector`. Sync wave 2. Deploys to `monitoring` namespace. Automated sync with prune and selfHeal. CreateNamespace=true. |
| **files** | `apps/otel-collector.yaml` |
| **phase** | 3 |
| **type** | create |
| **depends_on** | T3.1, T3.2 |
| **review_estimate** | 25 |
| **manual** | false |

**Verify:** ArgoCD shows `otel-collector` as Synced and Healthy. DaemonSet pods are Running on all nodes.

---

## Phase 4: OTel Gateway + Fluentbit (Wave 3)

### T4.1 — Create Gateway RBAC

| Field | Value |
|-------|-------|
| **id** | T4.1 |
| **title** | Create Gateway ServiceAccount and RBAC |
| **description** | Create a ServiceAccount, ClusterRole (get/list/watch on pods, namespaces, nodes, endpoints, services, deployments, replicasets, statefulsets, endpointslices), and ClusterRoleBinding for the OTel Collector Gateway. Extended permissions vs DaemonSet — needed for TargetAllocator pod discovery. |
| **files** | `infrastructure/monitoring/otel-gateway/rbac.yaml` |
| **phase** | 4 |
| **type** | create |
| **depends_on** | T2.1 |
| **review_estimate** | 40 |
| **manual** | false |

---

### T4.2 — Create Gateway OpenTelemetryCollector CR

| Field | Value |
|-------|-------|
| **id** | T4.2 |
| **title** | Create OTel Gateway StatefulSet CR |
| **description** | Create the `OpenTelemetryCollector` CR in `mode: statefulset`, 1 replica. Image: `otel/opentelemetry-collector-contrib:0.123.0`. Receivers: `otlp` (gRPC 4317, HTTP 4318), `prometheus` (via TargetAllocator). Processors: `memory_limiter` (800 MiB), `batch`, `k8sattributes`, `resource/cluster`. Exporters: `fluentforward` to `fluentbit-aggregator:24224`, `prometheusremotewrite` to `http://<PROMETHEUS_IP>:9090/api/v1/write`. Two pipelines: metrics (otlp+prometheus→prw) and logs (otlp→fluentforward). TargetAllocator enabled with `prometheusCR.enabled: true`. **Note:** Replace `<PROMETHEUS_IP>` placeholder with actual IP after Phase 5. |
| **files** | `infrastructure/monitoring/otel-gateway/collector.yaml` |
| **phase** | 4 |
| **type** | create |
| **depends_on** | T2.2, T4.1 |
| **review_estimate** | 100 |
| **manual** | false |

**Verify:** After ArgoCD sync: StatefulSet `collector-gateway-collector` shows 1/1 ready. TargetAllocator pod running.

---

### T4.3 — Create Gateway ArgoCD Application

| Field | Value |
|-------|-------|
| **id** | T4.3 |
| **title** | Create OTel Gateway Application |
| **description** | Create an ArgoCD Application manifest pointing to `infrastructure/monitoring/otel-gateway`. Sync wave 3. Deploys to `monitoring` namespace. Automated sync with prune and selfHeal. CreateNamespace=true. |
| **files** | `apps/otel-gateway.yaml` |
| **phase** | 4 |
| **type** | create |
| **depends_on** | T4.1, T4.2 |
| **review_estimate** | 25 |
| **manual** | false |

---

### T4.4 — Create Fluentbit ConfigMap

| Field | Value |
|-------|-------|
| **id** | T4.4 |
| **title** | Create Fluentbit configuration |
| **description** | Create a ConfigMap with `fluent-bit.conf` containing: `[SERVICE]` (flush 5, storage filesystem), `[INPUT]` forward on port 24224, `[OUTPUT]` S3 with `format parquet`, Hive-style key format `/raw/containers/year=%Y/month=%m/day=%d/hour=%H/$UUID.parquet`, buffer `total_file_size 5M` / `upload_timeout 5m`. Uses `${GARAGE_S3_ENDPOINT}` and `${GARAGE_BUCKET}` env vars. |
| **files** | `infrastructure/monitoring/fluentbit/configmap.yaml` |
| **phase** | 4 |
| **type** | create |
| **depends_on** | T2.1 |
| **review_estimate** | 55 |
| **manual** | false |

---

### T4.5 — Create Fluentbit Deployment, Service, and Secret template

| Field | Value |
|-------|-------|
| **id** | T4.5 |
| **title** | Create Fluentbit Deployment, Service, and Secret |
| **description** | Create three files: (1) Deployment: 1 replica, image `fluent/fluent-bit:4.1`, mounts ConfigMap at `/fluent-bit/etc`, emptyDir for buffers at `/var/fluent-bit/buffers`, env vars `GARAGE_S3_ENDPOINT` and `GARAGE_BUCKET`, envFrom `garage-credentials` Secret. (2) Service: ClusterIP, port 24224 (fluent-forward). (3) Secret template (gitignored): `garage-credentials` with `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` placeholders. |
| **files** | `infrastructure/monitoring/fluentbit/deployment.yaml`, `infrastructure/monitoring/fluentbit/service.yaml`, `infrastructure/monitoring/fluentbit/secret.yaml` |
| **phase** | 4 |
| **type** | create |
| **depends_on** | T4.4 |
| **review_estimate** | 70 |
| **manual** | false |

---

### T4.6 — Update .gitignore for Fluentbit secret

| Field | Value |
|-------|-------|
| **id** | T4.6 |
| **title** | Add Fluentbit secret.yaml to .gitignore |
| **description** | Add `infrastructure/monitoring/fluentbit/secret.yaml` to `.gitignore`. Note: the existing `.gitignore` already has `*secret*` pattern which would catch this file, but adding an explicit entry makes the intent clear and protects against future .gitignore changes. |
| **files** | `.gitignore` |
| **phase** | 4 |
| **type** | configure |
| **depends_on** | _none_ |
| **review_estimate** | 2 |
| **manual** | false |

---

### T4.7 — Create Fluentbit ArgoCD Application

| Field | Value |
|-------|-------|
| **id** | T4.7 |
| **title** | Create Fluentbit Application |
| **description** | Create an ArgoCD Application manifest pointing to `infrastructure/monitoring/fluentbit`. Sync wave 3. Deploys to `monitoring` namespace. Automated sync with prune and selfHeal. CreateNamespace=true. |
| **files** | `apps/fluentbit.yaml` |
| **phase** | 4 |
| **type** | create |
| **depends_on** | T4.4, T4.5, T4.6 |
| **review_estimate** | 25 |
| **manual** | false |

**Verify:** After ArgoCD sync: Deployment `fluentbit-aggregator` shows 1/1 ready. Service `fluentbit-aggregator` exists on port 24224.

---

## Phase 5: Prometheus VM (Manual)

> All tasks in this phase are performed on the Proxmox host / Prometheus VM. No repo changes.

### T5.1 — Provision Prometheus VM/LXC

| Field | Value |
|-------|-------|
| **id** | T5.1 |
| **title** | Provision Prometheus on Proxmox |
| **description** | Create a VM or LXC on Proxmox for Prometheus. Install Prometheus (native or Docker). Enable the `--web.enable-remote-write-receiver` flag. Set retention: `--storage.tsdb.retention.time=30d`, `--storage.tsdb.retention.size=5GB`. |
| **files** | _(none — Proxmox VM/LXC)_ |
| **phase** | 5 |
| **type** | provision |
| **depends_on** | _none_ |
| **review_estimate** | 0 |
| **manual** | true |

---

### T5.2 — Configure Prometheus (push-only)

| Field | Value |
|-------|-------|
| **id** | T5.2 |
| **title** | Configure Prometheus for remote_write only |
| **description** | Create `/etc/prometheus/prometheus.yml` with global settings only (scrape_interval, evaluation_interval). No `scrape_configs` section — all metrics arrive via remote_write from OTel Gateway. |
| **files** | _(none — Prometheus VM)_ |
| **phase** | 5 |
| **type** | configure |
| **depends_on** | T5.1 |
| **review_estimate** | 0 |
| **manual** | true |

**Verify:** `curl http://<PROMETHEUS_IP>:9090/-/healthy` returns 200.

**Note:** After this task completes, update `__PROMETHEUS_IP__` in `infrastructure/monitoring/otel-gateway/collector.yaml` (created in T4.2).

---

## Phase 6: Grafana VM (Manual)

> All tasks in this phase are performed on the Proxmox host / Grafana VM. No repo changes.

### T6.1 — Provision Grafana VM/LXC

| Field | Value |
|-------|-------|
| **id** | T6.1 |
| **title** | Provision Grafana on Proxmox |
| **description** | Create a VM or LXC on Proxmox for Grafana. Install Grafana (native or Docker). Install the DuckDB plugin (`grafana-duckdb-datasource`). Configure port 3000. |
| **files** | _(none — Proxmox VM/LXC)_ |
| **phase** | 6 |
| **type** | provision |
| **depends_on** | _none_ |
| **review_estimate** | 0 |
| **manual** | true |

---

### T6.2 — Configure Prometheus datasource in Grafana

| Field | Value |
|-------|-------|
| **id** | T6.2 |
| **title** | Add Prometheus datasource |
| **description** | Provision a Prometheus datasource in Grafana pointing to `http://<PROMETHEUS_IP>:9090`. Set as default datasource. |
| **files** | _(none — Grafana VM)_ |
| **phase** | 6 |
| **type** | configure |
| **depends_on** | T5.2, T6.1 |
| **review_estimate** | 0 |
| **manual** | true |

**Verify:** Grafana Explore → Prometheus → query `up` returns data.

---

### T6.3 — Configure DuckDB S3 datasource in Grafana

| Field | Value |
|-------|-------|
| **id** | T6.3 |
| **title** | Add DuckDB datasource for Parquet logs |
| **description** | Provision a DuckDB datasource in Grafana with the S3 plugin. InitQuery: `INSTALL httpfs; LOAD httpfs; SET s3_endpoint='<GARAGE_IP>:3900'; SET s3_region='garage'; SET s3_use_ssl=false; SET s3_url_style='path';`. Provide S3 credentials (access key, secret key from T1.2). Create the `logs()` macro for querying Hive-partitioned Parquet files. |
| **files** | _(none — Grafana VM)_ |
| **phase** | 6 |
| **type** | configure |
| **depends_on** | T1.3, T6.1 |
| **review_estimate** | 0 |
| **manual** | true |

**Verify:** Grafana Explore → Logs-DuckDB → SQL: `SELECT * FROM read_parquet('s3://logs/raw/containers/year=*/month=*/day=*/hour=*/*.parquet', hive_partitioning=true) LIMIT 10` returns log records (after logs have been flowing for a few minutes).

---

## Phase 7: End-to-End Verification

> All tasks in this phase verify the full pipeline. No repo changes.

### T7.1 — Verify ArgoCD sync status

| Field | Value |
|-------|-------|
| **id** | T7.1 |
| **title** | Verify all monitoring apps are Synced and Healthy |
| **description** | Check ArgoCD for all 4 monitoring Applications: `otel-operator` (wave 1), `otel-collector` (wave 2), `otel-gateway` (wave 3), `fluentbit` (wave 3). All must be `Synced` and `Healthy`. Verify wave ordering was respected (operator before collector, collector before gateway/fluentbit). |
| **files** | _(none — cluster state)_ |
| **phase** | 7 |
| **type** | verify |
| **depends_on** | T3.3, T4.3, T4.7 |
| **review_estimate** | 0 |
| **manual** | true |

---

### T7.2 — Verify metrics pipeline

| Field | Value |
|-------|-------|
| **id** | T7.2 |
| **title** | Verify metrics reach Prometheus |
| **description** | Query Prometheus UI for recent host metrics (e.g., `system_cpu_usage`, `system_memory_usage`). Verify time-series data points exist for all 3 nodes. Confirm no `scrape_configs` in Prometheus config (push-only). |
| **files** | _(none — Prometheus UI)_ |
| **phase** | 7 |
| **type** | verify |
| **depends_on** | T5.2, T7.1 |
| **review_estimate** | 0 |
| **manual** | true |

---

### T7.3 — Verify Parquet files in Garage

| Field | Value |
|-------|-------|
| **id** | T7.3 |
| **title** | Verify Parquet log files in Garage S3 |
| **description** | List objects in the `logs` bucket via Garage Admin API (port 3903) or S3 CLI. Verify Parquet files exist under `raw/containers/` with correct Hive-style partitions (`year=…/month=…/day=…/hour=…`). Download one file and verify it is valid Parquet. Check that records contain k8s metadata fields (`k8s_namespace_name`, `k8s_pod_name`, `k8s_container_name`). |
| **files** | _(none — Garage S3)_ |
| **phase** | 7 |
| **type** | verify |
| **depends_on** | T7.1 |
| **review_estimate** | 0 |
| **manual** | true |

---

### T7.4 — Verify Grafana visualization

| Field | Value |
|-------|-------|
| **id** | T7.4 |
| **title** | Verify Grafana queries metrics and logs |
| **description** | In Grafana: (1) Create a panel with PromQL (e.g., `system_cpu_usage`) — data renders. (2) Create a SQL panel with DuckDB using the `logs()` macro — log records are returned. Verify both datasources work end-to-end. |
| **files** | _(none — Grafana UI)_ |
| **phase** | 7 |
| **type** | verify |
| **depends_on** | T6.2, T6.3, T7.2, T7.3 |
| **review_estimate** | 0 |
| **manual** | true |

---

## Task Dependency Graph

```
T1.1 → T1.2 → T1.3 ─────────────────────────────────┐
                                                       │
T2.1 ──┬── T3.1 ──┬── T3.2 ──┬── T3.3 ──┐           │
       │          │          │           │           │
       └── T4.1 ──┴── T4.2 ──┬── T4.3 ──┤           │
                              │          │           │
T2.2 ────────────────────────┘          │           │
                                         │           │
T4.4 ──┬── T4.5 ──┬── T4.7 ──┤         │           │
       └──────────┘          │         │           │
T4.6 ────────────────────────┘         │           │
                                         │           │
                                         ▼           │
T5.1 → T5.2 ──┬── T6.2 ──┐            │           │
               │          │            │           │
T6.1 ─────────┴── T6.3 ──┤            │           │
                          │            │           │
                          ▼            ▼           ▼
                    T7.1 ◄─────────────┘           │
                    T7.2 ◄── T5.2 + T7.1          │
                    T7.3 ◄── T7.1                  │
                    T7.4 ◄── T6.2 + T6.3 + T7.2 + T7.3
```

## Summary

| Phase | Tasks | Review Lines | Manual | Repo Files |
|-------|-------|-------------|--------|------------|
| 1 — Proxmox Infrastructure | 3 | 0 | 3 | 0 |
| 2 — Namespace + Operator | 2 | 30 | 0 | 2 |
| 3 — OTel DaemonSet | 3 | 155 | 0 | 3 |
| 4 — Gateway + Fluentbit | 7 | 217 | 0 | 9 |
| 5 — Prometheus VM | 2 | 0 | 2 | 0 |
| 6 — Grafana VM | 3 | 0 | 3 | 0 |
| 7 — Verification | 4 | 0 | 4 | 0 |
| **Total** | **24** | **~402** | **14** | **14** |

### Files Created (non-manual)

| # | File | Phase |
|---|------|-------|
| 1 | `infrastructure/monitoring/namespace.yaml` | 2 |
| 2 | `apps/otel-operator.yaml` | 2 |
| 3 | `infrastructure/monitoring/otel-collector/rbac.yaml` | 3 |
| 4 | `infrastructure/monitoring/otel-collector/collector.yaml` | 3 |
| 5 | `apps/otel-collector.yaml` | 3 |
| 6 | `infrastructure/monitoring/otel-gateway/rbac.yaml` | 4 |
| 7 | `infrastructure/monitoring/otel-gateway/collector.yaml` | 4 |
| 8 | `apps/otel-gateway.yaml` | 4 |
| 9 | `infrastructure/monitoring/fluentbit/configmap.yaml` | 4 |
| 10 | `infrastructure/monitoring/fluentbit/deployment.yaml` | 4 |
| 11 | `infrastructure/monitoring/fluentbit/service.yaml` | 4 |
| 12 | `infrastructure/monitoring/fluentbit/secret.yaml` | 4 |
| 13 | `apps/fluentbit.yaml` | 4 |
| 14 | `.gitignore` (update) | 4 |
