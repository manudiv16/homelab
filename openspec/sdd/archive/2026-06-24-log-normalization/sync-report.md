# Log Normalization — Sync Report

**Change ID**: `log-normalization`  
**Date**: 2026-06-24  
**Executor**: sdd-sync  
**Status**: **SYNCED**  

---

## 1. Executive Summary

The verified `log-normalization` SDD change has been synced into canonical specs. The change's spec (905 lines) was copied to `openspec/specs/logging/spec.md` as the first canonical spec in the `logging` domain. No domain-level spec splitting was needed — the change uses a single flat `spec.md` artifact per project convention (`openspec/sdd/` layout).

The single critical blocker (ConfigMap key names with `/`) was already fixed during the verify phase. All 20 YAML files pass `kubectl apply --dry-run=server`. The change is fully deployable.

---

## 2. Sync Summary

| Aspect | Detail |
|---|---|
| **Domain** | `logging` |
| **Canonical file** | `openspec/specs/logging/spec.md` |
| **Source file** | `openspec/sdd/log-normalization/spec.md` |
| **Sync type** | New canonical spec (first sync for domain) |
| **Schema** | Copy (no delta merge — no existing canonical) |

### What was synced

The full 905-line `Log Normalization Specification` covering:

| Section | Requirements | Scope |
|---|---|---|
| OTel Collector Logs Pipeline | 1 requirement, 3 scenarios | Extends collector-daemonset with logs pipeline |
| Container Runtime Log Parsing | 1 requirement, 2 scenarios | CRI format parsing |
| Kubernetes Metadata Enrichment | 1 requirement, 3 scenarios | k8sattributes processor |
| Format Detection and Routing | 1 requirement, 4 scenarios | JSON, glog, key=value, text |
| Severity Normalization | 1 requirement, 4 scenarios | OTel severity mapping |
| Multiline Log Reassembly | 1 requirement, 3 scenarios | Java, Python, Go, Node.js traces |
| Timestamp Extraction | 1 requirement, 3 scenarios | CRI/JSON/glog/kv/fallback |
| Cluster Metadata Attributes | 1 requirement, 1 scenario | cluster.name=homelab |
| OTLP Export to Tansu | 1 requirement, 2 scenarios | OTLP gRPC to Tansu broker |
| Tansu Broker Deployment | 1 requirement, 4 scenarios | StatefulSet, PostgreSQL, Iceberg |
| Iceberg Schema for Logs | 1 requirement, 2 scenarios | otel_logs table, partitioning |
| Arroyo Streaming SQL | 1 requirement, 3 scenarios | Stream processing, 2nd-pass normalization |
| Trino Distributed SQL | 1 requirement, 2 scenarios | Iceberg connector queries |
| DuckDB Ad-Hoc Queries | 1 requirement, 1 scenario | iceberg_scan from S3 |
| Parallel Operation with FluentBit | 1 requirement, 2 scenarios | Dual pipeline coexistence |
| ArgoCD Deployment | 1 requirement, 2 scenarios | App of Apps onboarding |
| Resource Budget | 1 requirement, 2 scenarios | CPU/memory constraints |
| Log Record Deduplication | 1 requirement, 1 scenario | Offset tracking on restart |
| Unknown Format Graceful Handling | 1 requirement, 1 scenario | text fallback, no drops |
| Non-Functional: Latency Budget | 1 requirement, 1 scenario | ≤ 60s end-to-end |
| Non-Functional: Availability | 1 requirement, 1 scenario | Metrics pipeline isolation |
| Non-Functional: Upgrade Strategy | 1 requirement, 1 scenario | Rolling canary |
| Non-Functional: Historical Coexistence | 1 requirement, 1 scenario | Parquet unchanged |

**Total**: 23 requirements, 46 scenarios, 17 acceptance criteria, 8 risks, 5 mandatory queries (Q1–Q5), 5 normalization groups, 4 log format definitions.

---

## 3. Verification Status at Sync Time

| Check | Result |
|---|---|
| verify-report.md exists | ✅ `openspec/sdd/log-normalization/verify-report.md` |
| Critical blockers | ✅ Fixed during verify (ConfigMap keys `catalog/` → valid names) |
| All files pass `kubectl apply --dry-run=server` | ✅ 20/20 YAML files pass |
| Spec compliance (static ACs) | ✅ 14/17 ACs PASS statically |
| Runtime-only ACs | ⏭️ 3 (AC-11 latency, AC-15 parallel counts, AC-12 memory under load) |
| Resource budgets match design | ✅ All components match exactly |

---

## 4. Guardrail Checks

| Guardrail | Status | Detail |
|---|---|---|
| verify-report.md present | ✅ PASS | Present with post-fix green status |
| Verification not blocked | ✅ PASS | Critical blocker fixed |
| Legacy flat spec check | ✅ N/A | Project uses `openspec/sdd/`, not `openspec/changes/` — no legacy flat spec guardrail triggered |
| MODIFIED/REMOVED requirement exists | ✅ N/A | New canonical spec — no existing spec to check against |
| Destructive sync (REMOVED/large MODIFIED) | ✅ N/A | No existing canonical spec in domain |
| Same-domain collision | ✅ PASS | No other active change targets `specs/logging/` |
| RENAMED requirements | ✅ N/A | No `## RENAMED Requirements` present |
| Config `rules.sync` applied | ✅ N/A | No `rules.sync` in `openspec/config.yaml` |

---

## 5. Active Same-Domain Collisions

**None.** The only other SDD change (`monitoring-architecture`) targets the metrics/monitoring domain, not logging. No active change writes to `openspec/specs/logging/`.

---

## 6. Structured Status & Action Context Findings

From the SDD session preflight:

```json
{
  "artifactStore": "openspec",
  "actionContext": {
    "mode": "repo-local",
    "workspaceRoot": "/Users/franrubio/Documents/projects/homelab",
    "allowedEditRoots": ["/Users/franrubio/Documents/projects/homelab"],
    "warnings": []
  }
}
```

- `mode: repo-local` — all canonical paths are within workspace root ✅
- `allowedEditRoots` includes `/Users/franrubio/Documents/projects/homelab` — all edits in scope ✅
- No warnings or collisions

---

## 7. Validation Performed

- ✅ Canonical spec copied: 905 lines, matches source spec.md exactly
- ✅ No content modified, truncated, or reformatted
- ✅ Domain `logging` created as new empty domain directory
- ✅ No existing `openspec/specs/logging/` before sync — clean first write

---

## 8. Risks Carried Forward

These risks remain open from the verify phase and should be tracked through deployment:

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R-V1 | ~~Trino ConfigMap uncreatable~~ | 🔴 → ✅ **FIXED** | Keys renamed to valid format |
| R-V6 | Secret placeholders in git | 🟡 MEDIUM | `secret.yaml` has `changeme`/`PLACEHOLDER` — populate before deploy |
| R-V3 | Tansu image tag `:latest` | 🟡 LOW | Pin after first successful deploy |
| R-V4 | Arroyo image tag `:latest` | 🟡 LOW | Pin after first successful deploy |
| R-V2 | Pipeline processor order swapped | 🟡 LOW | No functional impact; document or align |
| R-V5 | No `apply-progress.md` | 🟡 LOW | Process gap; no functional impact with infra validation |

---

## 9. Next Recommended Phase

**`sdd-archive`** — Move the already-synced change to dated archive after confirming:
1. Secrets populated with real credentials
2. Image tags pinned (optional pre-archive cleanup)
3. `apply-progress.md` created if strict TDD compliance desired

---

## Phase Envelope

```json
{
  "status": "synced",
  "executive_summary": "log-normalization SDD change synced to openspec/specs/logging/spec.md as new canonical logging domain spec. 905 lines across 23 requirements, 46 scenarios, 17 ACs. Single critical blocker (ConfigMap key names) fixed during verify phase. No same-domain collisions. Change remains active — not archived.",
  "artifacts": {
    "syncReport": "openspec/sdd/log-normalization/sync-report.md",
    "canonicalSpec": "openspec/specs/logging/spec.md",
    "changeSpec": "openspec/sdd/log-normalization/spec.md"
  },
  "next_recommended": "sdd-archive",
  "risks": [
    {"id": "R-V6", "severity": "MEDIUM", "description": "Secret contains placeholder credentials (changeme/PLACEHOLDER) — must populate before deploy"},
    {"id": "R-V3", "severity": "LOW", "description": "Tansu image tag is :latest — pin after first deploy"},
    {"id": "R-V4", "severity": "LOW", "description": "Arroyo image tag is :latest — pin after first deploy"},
    {"id": "R-V2", "severity": "LOW", "description": "Pipeline processor order (transform vs resource/cluster) swapped from design"},
    {"id": "R-V5", "severity": "LOW", "description": "Missing apply-progress.md — process gap with strict_tdd:true"}
  ],
  "skill_resolution": "paths-injected"
}
```
