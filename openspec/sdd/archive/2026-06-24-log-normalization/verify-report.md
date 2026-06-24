# Log Normalization — Verification Report

**Change ID**: `log-normalization`  
**Date**: 2026-06-24  
**Verifier**: sdd-verify executor  
**Status**: **FAIL** — 1 CRITICAL blocker, 3 WARNINGs, 1 INFO  

---

## 1. Executive Summary

**Overall**: FAIL (blocked by invalid ConfigMap key)  
**YAML validity**: 1 CRITICAL failure (trino-catalog-configmap.yaml), all other files PASS  
**Spec compliance**: 14/17 ACs verified statically, 1 blocked by YAML issue (AC-9), 2 runtime-only (AC-11, AC-15)  
**Design compliance**: Strong alignment with 3 minor deviations noted  
**Task completion**: 20/20 git-managed tasks have corresponding files, no `- [ ]` checkboxes in tasks.md  
**Strict TDD**: Active in config but no apply-progress.md exists, no test files in project — TDD not applicable to this infrastructure-as-code project  
**Review workload**: 1,013 lines total vs 650–850 forecast, slight overrun (~20%)  

---

## 2. YAML Validation Results

### kubectl apply --dry-run=server

| File Group | Command | Result | Details |
|---|---|---|---|
| `infrastructure/tansu/` (all) | `kubectl apply --dry-run=server -f infrastructure/tansu/` | ✅ **PASS** | All 8 resources created: postgres-statefulset, postgres-service, secret, tansu-statefulset, tansu-service, serviceaccount, role, rolebinding |
| `infrastructure/monitoring/otel-collector/collector.yaml` | `kubectl apply --dry-run=server -f` | ✅ **PASS** | OpenTelemetryCollector configured (dry run) |
| `infrastructure/iceberg-query/` | `kubectl apply --dry-run=server -f` | ❌ **FAIL** | Trino deployment + service PASS. ConfigMap REJECTED — invalid data keys |
| `infrastructure/arroyo/` | `kubectl apply --dry-run=server -f` | ✅ **PASS** | Arroyo deployment, service, configmap all created |
| `apps/tansu.yaml` | `kubectl apply --dry-run=server -f` | ✅ **PASS** | ArgoCD Application created |
| `apps/iceberg-query.yaml` | `kubectl apply --dry-run=server -f` | ✅ **PASS** | ArgoCD Application created |
| `apps/arroyo.yaml` | `kubectl apply --dry-run=server -f` | ✅ **PASS** | ArgoCD Application created |
| `apps/otel-collector.yaml` | `kubectl apply --dry-run=server -f` | ✅ **PASS** | Existing ArgoCD Application configured |

### ❌ CRITICAL: trino-catalog-configmap.yaml — Invalid ConfigMap Keys

**Error**:
```
The ConfigMap "trino-catalog-config" is invalid:
* data[catalog/iceberg.properties]: Invalid value: "catalog/iceberg.properties": a valid config key must consist of alphanumeric characters, '-', '_' or '.'
* data[catalog/memory.properties]: Invalid value: "catalog/memory.properties": a valid config key must consist of alphanumeric characters, '-', '_' or '.'
```

**Root cause**: Kubernetes ConfigMap `data` keys must match `[-._a-zA-Z0-9]+`. The `/` character in `catalog/iceberg.properties` and `catalog/memory.properties` is not permitted.

**Impact**: The Trino pod cannot start because the catalog configuration is in an uncreatable ConfigMap. This blocks:
- AC-9 (Iceberg table queryable)
- All Trino query validation (Q1–Q5)
- T3.1 (Trino Deployment) — pod will fail to mount catalog config

**Required fix**: Either:
1. Rename keys to valid names (e.g., `iceberg.properties`, `memory.properties`) and adjust Trino to use a different catalog directory mapping (e.g., mount to `/etc/trino/catalog/` where each key becomes a file), OR
2. Split into separate ConfigMaps and use projected volumes, OR
3. Use a `binaryData` approach or init container to write catalog files

---

## 3. Spec Compliance Matrix

| AC # | Criterion | Implementation Mapping | Status |
|---|---|---|---|
| AC-1 | All namespaces covered | `filelog.include: [/var/log/containers/*.log]` — covers all namespaces via wildcard | ✅ **PASS** |
| AC-2 | JSON format parsed | `json_parser` operator: `if: body startsWith "{"`, `parse_to: attributes` | ✅ **PASS** |
| AC-3 | glog severity extracted | `regex_parser` (glog-parser): regex captures severity_letter (I/W/E/F), maps to info/warn/error/fatal | ✅ **PASS** |
| AC-4 | Key=Value format parsed | `key_value_parser` (kv-parser): `if: body matches "\\w+=\\S+.*\\w+=\\S+.*\\w+=\\S+"` | ✅ **PASS** |
| AC-5 | Plain text preserved | OTTL Group 1: default `set(attributes["log.format"], "text")`; body untouched for text format | ✅ **PASS** |
| AC-6 | Multiline reassembled | `multiline.line_start_pattern`: covers Java (ISO date), glog, Python (Traceback), Go (panic/goroutine); `flush_after: 5s` | ✅ **PASS** |
| AC-7 | Severity default | OTTL Group 2: `set(severity_text, "INFO")` + `set(severity_number, 9)` executed unconditionally as default | ✅ **PASS** |
| AC-8 | K8s metadata populated | `k8sattributes` processor extracts: k8s.namespace.name, k8s.pod.name, k8s.container.name, k8s.node.name, k8s.deployment.name | ✅ **PASS** |
| AC-9 | Iceberg table queryable | ❌ Cannot verify — Trino ConfigMap is uncreatable (see §2) | ❌ **BLOCKED** |
| AC-10 | Metrics pipeline unaffected | Metrics pipeline (hostmetrics + kubeletstats → memory_limiter + k8sattributes + resource/cluster + batch → otlp) unchanged from design | ✅ **PASS** |
| AC-11 | Latency ≤ 60s | Runtime-only check. batch/logs timeout=5s + OTLP export + Tansu write — design targets <60s end-to-end. Not verifiable statically. | ⏭️ **RUNTIME** |
| AC-12 | Collector memory < 600Mi | `resources.limits.memory: 600Mi`, `memory_limiter.limit_mib: 500` (500+100 headroom) | ✅ **PASS** |
| AC-13 | ArgoCD all green | All 3 new apps (tansu wave 3, iceberg-query wave 4, arroyo wave 4) use `automated: prune+selfHeal`, `CreateNamespace=true` | ✅ **PASS** (syntax) |
| AC-14 | FluentBit unchanged | No log-normalization file touches `infrastructure/monitoring/fluentbit/`. Pre-existing comment-only change (Garage→Scaleway) is unrelated. | ✅ **PASS** |
| AC-15 | Parallel log counts | Runtime-only check (requires 24h of operation). Not verifiable statically. | ⏭️ **RUNTIME** |
| AC-16 | Unknown formats not dropped | OTTL: no `drop()` statements anywhere. All paths set log.format and preserve body. Exporter has retry queue, no drop logic for unrecognized formats. | ✅ **PASS** |
| AC-17 | Tansu recovery | StatefulSet with `volumeClaimTemplates` (5Gi PVC at `/var/lib/tansu`). Filelog `storage: file_storage` with hostPath persistence for offset tracking. | ✅ **PASS** |

**Summary**: 14/17 ACs PASS statically, 2 are runtime-only (AC-11, AC-15), 1 is BLOCKED (AC-9) by the ConfigMap issue.

---

## 4. Design Compliance

### 4.1 OTel Collector Configuration (Design §1)

| Design Element | Implementation | Match |
|---|---|---|
| filelog receiver with 4 operators | ✅ container, json_parser, regex_parser, key_value_parser — all present with correct `if:` predicates | ✅ |
| multiline line_start_pattern | ✅ `'^(\d{4}-\d{2}-\d{2}\|[IWEF]\d{4}\|Traceback\|panic\|goroutine \d+)'` — covers Java, glog, Python, Go | ✅ |
| transform/normalize-logs OTTL Groups 1–5 | ✅ Format detection (4 statements), severity normalization (~26 statements), timestamp normalization (~6), body cleanup (~6), attribute cleanup (5 delete_key) | ✅ |
| batch/logs processor | ✅ timeout 5s, send_batch_size 512, send_batch_max_size 1024 | ✅ |
| otlp/tansu exporter | ✅ endpoint tansu-broker.monitoring.svc.cluster.local:4317, retry_on_failure, sending_queue | ✅ |
| resource limits | ✅ CPU 150m/500m, Memory 200Mi/600Mi | ✅ |
| memory_limiter | ✅ limit_mib 500, spike_limit_mib 150 | ✅ |
| file_storage extension | ✅ directory /var/lib/otel, timeout 1s, compaction settings | ✅ |
| Volumes/volumeMounts | ✅ varlog, varlibdockercontainers, otel-registry | ✅ |

#### ⚠️ WARNING: Pipeline Processor Order Deviation

| Position | Design §1.1 (YAML) | Implementation | Delta |
|---|---|---|---|
| 1 | memory_limiter | memory_limiter | ✅ same |
| 2 | k8sattributes | k8sattributes | ✅ same |
| 3 | **transform/normalize-logs** | **resource/cluster** | ⚠️ swapped |
| 4 | **resource/cluster** | **transform/normalize-logs** | ⚠️ swapped |
| 5 | batch/logs | batch/logs | ✅ same |

**Analysis**: The order of `transform/normalize-logs` and `resource/cluster` is swapped. `resource/cluster` only adds `cluster.name=homelab` resource attribute and does not depend on transform output, so functional impact is **none**. However, the design's pipeline stage diagram places transform before resource/cluster. Low risk, but noted as a deviation.

### 4.2 Tansu Broker (Design §2)

| Design Element | Implementation | Match |
|---|---|---|
| StatefulSet | ✅ tansu-broker, 1 replica, 5Gi PVC | ✅ |
| Environment vars | ✅ TANSU_STORAGE_ENGINE=postgres, Kafka listener, OTLP listener, Iceberg config, S3 creds | ✅ |
| Secret references | ✅ postgres-dsn, s3-access-key, s3-secret-key from tansu-secrets | ✅ |
| Resources | ✅ 100m/256Mi req, 500m/512Mi limit | ✅ |
| Probes | ✅ TCP readiness/liveness on port 9092 | ✅ |
| Service | ✅ ClusterIP, ports 4317 (otlp-grpc), 9092 (kafka) | ✅ |
| PostgreSQL | ✅ postgres:16-alpine StatefulSet, 2Gi PVC, pg_isready probe | ✅ |
| RBAC | ✅ ServiceAccount tansu-broker + Role + RoleBinding | ✅ |

#### ℹ️ INFO: Additional Ports Not in Design

- Tansu StatefulSet exposes port **9100** (metrics) — not in design §2.3. Tansu Service also includes this port. **Harmless addition**.

#### ℹ️ INFO: No Tansu ConfigMap

- Design §2.2 lists `configmap.yaml` for Tansu but no task was assigned for it and none was created. Tansu uses env vars exclusively, which is a valid simplification.

### 4.3 Trino (Design §4.1)

| Design Element | Implementation | Match |
|---|---|---|
| Deployment | ✅ trinodb/trino:468, single replica | ✅ |
| Resources | ✅ 250m/512Mi req, 1/1Gi limit | ✅ |
| Service | ✅ ClusterIP port 8080 | ✅ |
| Probes | ✅ HTTP GET /v1/info (initial delays adjusted: 30s/60s vs design 10s/30s — **safer**) | ✅ |
| Catalog ConfigMap | ❌ Invalid keys (see §2) | ❌ FAIL |

#### ❌ CRITICAL: Trino Catalog ConfigMap Blocked

The design §4.1 specifies Trino catalog config as a ConfigMap. Implementation exists but is uncreatable due to `/` in keys. See §2 for details and fix options.

### 4.4 Arroyo (Design §4.3)

| Design Element | Implementation | Match |
|---|---|---|
| Deployment | ✅ ghcr.io/arroyosystems/arroyo:latest, single replica | ✅ |
| Resources | ✅ 100m/256Mi req, 500m/512Mi limit | ✅ |
| Service | ✅ ClusterIP port 8000 | ✅ |
| Probes | ✅ HTTP GET /api/v1/status | ✅ |
| Streaming SQL ConfigMap | ✅ Kafka source, tumbling window, Iceberg sink | ✅ |

### 4.5 ArgoCD Applications (Design §5)

| Application | Wave | repoURL | targetRevision | prune | selfHeal | Status |
|---|---|---|---|---|---|---|
| tansu | 3 | github.com/manudiv16/homelab.git | main | ✅ true | ✅ true | ✅ PASS |
| iceberg-query | 4 | github.com/manudiv16/homelab.git | main | ✅ true | ✅ true | ✅ PASS |
| arroyo | 4 | github.com/manudiv16/homelab.git | main | ✅ true | ✅ true | ✅ PASS |
| otel-collector (existing) | 2 | github.com/manudiv16/homelab.git | main | ✅ true | ✅ true | ✅ PASS |

All apps use `CreateNamespace=true`, `directory.recurse: true`, and target namespace `monitoring`. Sync wave ordering matches design: otel-collector (2) → tansu (3) → arroyo+iceberg-query (4).

---

## 5. Resource Budget Check

### Design §6 Budget vs Implementation

| Component | Resource | Design | Implementation | Match |
|---|---|---|---|---|
| **OTel Collector** | CPU req | 150m | 150m | ✅ |
| | CPU limit | 500m | 500m | ✅ |
| | Memory req | 200Mi | 200Mi | ✅ |
| | Memory limit | 600Mi | 600Mi | ✅ |
| | memory_limiter limit_mib | 500 | 500 | ✅ |
| | memory_limiter spike_limit_mib | 150 | 150 | ✅ |
| **Tansu Broker** | CPU req | 100m | 100m | ✅ |
| | CPU limit | 500m | 500m | ✅ |
| | Memory req | 256Mi | 256Mi | ✅ |
| | Memory limit | 512Mi | 512Mi | ✅ |
| | PVC | 5Gi | 5Gi | ✅ |
| **PostgreSQL** | CPU req | 50m | 50m | ✅ |
| | CPU limit | 250m | 250m | ✅ |
| | Memory req | 128Mi | 128Mi | ✅ |
| | Memory limit | 256Mi | 256Mi | ✅ |
| | PVC | 2Gi | 2Gi | ✅ |
| **Trino** | CPU req | 250m | 250m | ✅ |
| | CPU limit | 1000m | 1 (1000m) | ✅ |
| | Memory req | 512Mi | 512Mi | ✅ |
| | Memory limit | 1Gi | 1Gi | ✅ |
| **Arroyo** | CPU req | 100m | 100m | ✅ |
| | CPU limit | 500m | 500m | ✅ |
| | Memory req | 256Mi | 256Mi | ✅ |
| | Memory limit | 512Mi | 512Mi | ✅ |

**All resource budgets match the design exactly.** No deviations.

---

## 6. Task Completion Status

| Task | Description | File(s) | Status |
|---|---|---|---|
| T1.1 | PostgreSQL StatefulSet + Service | `postgres-statefulset.yaml`, `postgres-service.yaml` | ✅ Complete |
| T1.2 | Tansu Secret | `secret.yaml` (4 keys: postgres-dsn, postgres-password, s3-access-key, s3-secret-key) | ✅ Complete |
| T1.3 | Tansu Broker StatefulSet | `statefulset.yaml` | ✅ Complete |
| T1.4 | Tansu Service | `service.yaml` | ✅ Complete |
| T1.5 | Tansu RBAC | `rbac.yaml` (SA + Role + RoleBinding) | ✅ Complete |
| T1.6 | ArgoCD App for Tansu | `apps/tansu.yaml` | ✅ Complete |
| T2.1 | Volumes + file_storage extension | `collector.yaml` (volumes, volumeMounts, extensions) | ✅ Complete |
| T2.2 | filelog receiver + operators | `collector.yaml` (4 operators, multiline, include/exclude) | ✅ Complete |
| T2.3 | transform/normalize-logs processor | `collector.yaml` (42 OTTL statements, 5 groups) | ✅ Complete |
| T2.4 | batch/logs + otlp/tansu exporter | `collector.yaml` (processor + exporter + retry/sending_queue) | ✅ Complete |
| T2.5 | Logs pipeline in service.pipelines | `collector.yaml` (7-element pipeline definition) | ✅ Complete |
| T2.6 | Resource limits increase | `collector.yaml` (150m/200Mi req, 500m/600Mi limit, memory_limiter 500/150) | ✅ Complete |
| T3.1 | Trino Deployment | `trino-deployment.yaml` | ✅ Complete* |
| T3.2 | Trino Service | `trino-service.yaml` | ✅ Complete |
| T3.3 | Trino Catalog ConfigMap | `trino-catalog-configmap.yaml` | ⚠️ **yaml exists but uncreatable** |
| T3.4 | ArgoCD App for Iceberg Query | `apps/iceberg-query.yaml` | ✅ Complete |
| T3.5 | Arroyo Deployment | `deployment.yaml` | ✅ Complete |
| T3.6 | Arroyo Service | `service.yaml` | ✅ Complete |
| T3.7 | Arroyo ConfigMap | `configmap.yaml` (streaming SQL pipeline) | ✅ Complete |
| T3.8 | ArgoCD App for Arroyo | `apps/arroyo.yaml` | ✅ Complete |

*T3.1 is complete as a file but blocked at runtime by T3.3's ConfigMap issue.

**No `- [ ]` unchecked checkboxes found** in `tasks.md`. The tasks file uses header-based task labeling (`### T1.1 — ...`) without checkbox syntax. All 20 git-managed tasks have corresponding implementation files.

### Unchecked Implementation Task Markers

**None found.** A scan of `openspec/sdd/log-normalization/tasks.md` for `^\s*- \[ \]` returned zero matches. All tasks are header-based, not checkbox-based.

---

## 7. Review Workload Verification

| Metric | Forecast | Actual | Status |
|---|---|---|---|
| Total changed lines | 650–850 | **1,013** | ⚠️ **+20% overrun** |
| PR 1 (Tansu) estimate | ~250 | ~281 | Within ±15% |
| PR 2 (Collector) estimate | ~300 | ~223 (collector diff) | Under forecast |
| PR 3 (Query) estimate | ~250 | ~359 | ⚠️ **+44% overrun** |
| Chained PRs used | Yes (stacked-to-main) | ✅ 3 PRs, stacked | ✅ |
| 400-line budget per PR | All 3 under | ✅ Largest is PR 3 at ~359 | ✅ Budget respected per-PR |

### PR Boundary Check

| PR | Covers Tasks | Files Match | Boundary Leak? |
|---|---|---|---|
| PR 1 | T1.1–T1.6 | `infrastructure/tansu/` + `apps/tansu.yaml` | ✅ Clean |
| PR 2 | T2.1–T2.6 | `infrastructure/monitoring/otel-collector/collector.yaml` only | ✅ Clean |
| PR 3 | T3.1–T3.8 | `infrastructure/iceberg-query/` + `infrastructure/arroyo/` + `apps/` | ✅ Clean |

**No scope creep detected.** Each PR is self-contained within its assigned slice. The 1,013-line total exceeds the 650–850 forecast mainly due to the Trino ConfigMap including full base configs (config.properties, jvm.config, node.properties) beyond the catalog properties, and the Arroyo ConfigMap containing the full streaming SQL pipeline (~100 lines). This is acceptable expansion within the "ask-on-risk" delivery strategy.

---

## 8. Strict TDD Compliance

`openspec/config.yaml` sets `strict_tdd: true`.

| Requirement | Finding |
|---|---|
| apply-progress.md exists | ❌ **MISSING** — No `openspec/sdd/log-normalization/apply-progress.md` found |
| TDD Cycle Evidence table | ❌ Cannot verify — no apply-progress.md |
| Test files | N/A — Project `testing.runner: none`. This is an infrastructure-as-code project validated via `kubectl apply --dry-run=server`, ArgoCD sync, and cluster observation |
| GREEN test runs | N/A — No test runner configured |

**Assessment**: Strict TDD is configured but the project has no test runner and relies on infrastructure validation (`kubectl --dry-run`, ArgoCD sync). The `apply-progress.md` artifact is missing from the SDD pipeline. This is a **process gap** but not a functional blocker for infrastructure manifests. The YAML validation results in §2 serve as the equivalent of "tests passing" for this domain.

⚠️ **WARNING**: `strict_tdd: true` in config but no `apply-progress.md` and no test framework. Either disable strict TDD in config or document that infrastructure-as-code validation (`kubectl --dry-run=server`) substitutes for traditional TDD.

---

## 9. Risks Discovered

| # | Risk | Severity | Description |
|---|---|---|---|
| R-V1 | **Trino ConfigMap uncreatable** | 🔴 CRITICAL | ConfigMap key `catalog/iceberg.properties` contains `/` — Kubernetes rejects it. Trino cannot start. Blocks AC-9 and Phase 4 verification. |
| R-V2 | **Pipeline processor order swapped** | 🟡 LOW | `transform/normalize-logs` and `resource/cluster` positions swapped. No functional impact; `resource/cluster` only sets `cluster.name`. |
| R-V3 | **Tansu image tag: latest** | 🟡 LOW | Design open decision D1 flags this. Tag is `ghcr.io/rustyconover/tansu:latest`. Should pin after first successful deploy. |
| R-V4 | **Arroyo image tag: latest** | 🟡 LOW | Same floating tag risk. Should pin after first deploy. |
| R-V5 | **No apply-progress.md** | 🟡 LOW | Missing artifact. Process gap but no functional impact. |
| R-V6 | **Secret placeholders in git** | 🟡 MEDIUM | `secret.yaml` contains `changeme` password and `PLACEHOLDER` S3 keys. Real credentials must be injected before deployment. |
| R-V7 | **Metrics port 9100 on Tansu** | ℹ️ INFO | Not in design but present in implementation. Not harmful but could conflict if Tansu doesn't expose metrics on that port. |

---

## 10. Required Fixes

### 🔴 CRITICAL — Must Fix Before Deploy

1. **Fix `trino-catalog-configmap.yaml` ConfigMap keys** — Rename `catalog/iceberg.properties` and `catalog/memory.properties` to valid keys. Options:
   - **Option A**: Rename keys to `iceberg.properties` and `memory.properties`. Then adjust Trino deployment to mount catalog configs as individual files via subPath into a `/etc/trino/catalog/` directory. This requires either a directory mount approach or a script init container.
   - **Option B**: Split the ConfigMap into two: one for Trino base config (`config.properties`, `jvm.config`, `node.properties`) and one for catalog files, using subPath mounts.
   - **Option C (simplest)**: Create a separate ConfigMap for each catalog file, mount each via subPath to the correct path. E.g., `trino-catalog-iceberg` ConfigMap with key `iceberg.properties` mounted to `/etc/trino/catalog/iceberg.properties`.

### 🟡 RECOMMENDED — Fix Before Archive

2. **Pin Tansu and Arroyo image tags** — Replace `:latest` with specific tags after first successful deploy (design open decision D1).
3. **Document or fix pipeline processor order** — Either align `resource/cluster` after `transform/normalize-logs` as shown in design, or document the rationale for the current order.
4. **Create `apply-progress.md`** — Required by `strict_tdd: true` in config. Record implementation progress, review workload adherence, and TDD cycle evidence (YAML validation results).

---

## 11. Verification Commands Executed

```bash
# YAML validation — Tansu infrastructure
kubectl apply --dry-run=server -f infrastructure/tansu/
# → All 8 resources created (dry run)

# YAML validation — OTel Collector
kubectl apply --dry-run=server -f infrastructure/monitoring/otel-collector/collector.yaml
# → opentelemetrycollector configured (dry run)

# YAML validation — Iceberg Query (Trino)
kubectl apply --dry-run=server -f infrastructure/iceberg-query/
# → trino Deployment + Service PASS. ConfigMap FAILED (invalid data keys)

# YAML validation — Arroyo
kubectl apply --dry-run=server -f infrastructure/arroyo/
# → All 3 resources created (dry run)

# YAML validation — ArgoCD Applications
kubectl apply --dry-run=server -f apps/tansu.yaml
kubectl apply --dry-run=server -f apps/iceberg-query.yaml
kubectl apply --dry-run=server -f apps/arroyo.yaml
kubectl apply --dry-run=server -f apps/otel-collector.yaml
# → All 4 Application resources created/configured (dry run)

# FluentBit unchanged verification
git diff --stat HEAD -- infrastructure/monitoring/fluentbit/
# → 1 file changed (events-configmap.yaml, pre-existing comment change, unrelated)

# Task checkbox scan
grep '^\s*- \[' openspec/sdd/log-normalization/tasks.md
# → No matches (no checkbox-format tasks)
```

---

## 12. Archive Readiness

**NOT READY FOR ARCHIVE** — 1 CRITICAL blocker (trino-catalog-configmap.yaml invalid keys) prevents the Trino query layer from deploying. The OTel Collector (PR 2) and Tansu infrastructure (PR 1) are independently deployable and could be archived separately, but the full change cannot be archived until the ConfigMap is fixed.

---

## Phase Envelope

```json
{
  "status": "FAIL",
  "executive_summary": "1 CRITICAL blocker: trino-catalog-configmap.yaml has invalid ConfigMap data keys ('catalog/iceberg.properties' and 'catalog/memory.properties' contain '/'). All other YAML files pass server dry-run validation. 14/17 ACs PASS statically, 2 are runtime-only (AC-11, AC-15), 1 blocked (AC-9). 20/20 git-managed tasks have implementation files. 3 WARNINGs (pipeline order swap, floating image tags, missing apply-progress.md), 1 INFO.",
  "artifacts": {
    "verifyReport": "openspec/sdd/log-normalization/verify-report.md"
  },
  "next_recommended": "Fix trino-catalog-configmap.yaml invalid keys, then re-run kubectl apply --dry-run=server. After ConfigMap fix, proceed to Phase 4 manual verification (T4.1–T4.8).",
  "risks": [
    {"id": "R-V1", "severity": "CRITICAL", "description": "Trino ConfigMap uncreatable — invalid data key characters"},
    {"id": "R-V6", "severity": "MEDIUM", "description": "Secret contains placeholder credentials — must be populated before deploy"},
    {"id": "R-V3", "severity": "LOW", "description": "Tansu image tag is :latest — pin after first deploy"},
    {"id": "R-V4", "severity": "LOW", "description": "Arroyo image tag is :latest — pin after first deploy"},
    {"id": "R-V2", "severity": "LOW", "description": "Pipeline processor order (transform vs resource/cluster) swapped from design YAML"}
  ],
  "skill_resolution": "paths-injected"
}
```

---

## Post-Verify Fix Applied

### Blocker: ConfigMap key names with `/`

**Finding**: `trino-catalog-configmap.yaml` used keys `catalog/iceberg.properties` and `catalog/memory.properties` — invalid for Kubernetes ConfigMaps (key regex: `[-._a-zA-Z0-9]+`).

**Fix applied**:
1. Renamed keys to `iceberg.properties` and `memory.properties`
2. Updated `trino-deployment.yaml` to use `items` selector in the volume to mount only catalog files at `/etc/trino/catalog/`
3. All files now pass `kubectl apply --dry-run=server`

**Verdict after fix**: ✅ All 20 files pass validation. All 3 PRs are deployable.

### Final Stats

| Check | Result |
|---|---|
| Tansu infra (6 yamls + app) | ✅ dry-run=server |
| OTel Collector (collector.yaml) | ✅ dry-run=server |
| Query layer (7 yamls + 2 apps) | ✅ dry-run=server |
| FluentBit pipeline | ✅ untouched (verified) |
| Spec compliance | 14/17 ACs static pass, 3 runtime-only |
| Resource budgets | ✅ match design exactly |
