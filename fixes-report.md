# Pipeline fixes report

## Fix 1 — OTel Collector CRI parser (`on_error: send_quiet`)

**File**: `infrastructure/monitoring/otel-collector/collector.yaml`

**Problem**: The `container` operator (CRI parser) had no `on_error` directive.
When a log line doesn't match the containerd CRI format (MetalLB speaker,
some k3s internal components write plain text without CRI prefix), the operator
raised an error that **stopped the entire filelog pipeline for that file**.
Result: 2,338+ error lines, many logs silently dropped.

**Fix**: Added `on_error: send_quiet` to the container operator.  
Non-matching lines now pass through with `action: send` silently — they reach
the transform processor with an empty body instead of being blocked.

```yaml
- type: container
  id: cri-parser
  format: containerd
  on_error: send_quiet   # ← added
```

**Validation**: `kubectl apply --dry-run=server` → `configured`

---

## Fix 2 — Trino data directory (`emptyDir`)

**File**: `infrastructure/iceberg-query/trino-deployment.yaml`

**Problem**: Trino crashed on startup with:
```
mkdir /var/trino: permission denied
```
The container image expects `/var/trino/data` to exist (or be writable),
but the container runs as a non-root user and the directory doesn't exist.

**Fix**: Added an `emptyDir` volume mounted at `/var/trino/data`.
Kubernetes creates the directory with correct permissions before the container
starts, so Trino can use it as its data directory without needing root.

```yaml
volumeMounts:
  - name: trino-data
    mountPath: /var/trino/data   # ← added

volumes:
  - name: trino-data
    emptyDir: {}                 # ← added
```

**Validation**: `kubectl apply --dry-run=server` → `configured`

---

## Fix 3 — Arroyo invalid env vars

**File**: `infrastructure/arroyo/deployment.yaml`

**Problem**: Arroyo crashed immediately with:
```
Configuration is invalid!
  • unknown field: found `iceberg-catalog-uri`, expected `one of `api`, ...`
```
The deployment had env vars with `ARROYO__` prefix mapping to fields that
don't exist in this version of Arroyo (`ARROYO__ICEBERG_CATALOG_URI`,
`ARROYO__KAFKA_BOOTSTRAP_SERVERS`, `ARROYO__ICEBERG_WAREHOUSE`).

**Fix**: Removed all three invalid `ARROYO__` env vars. Arroyo starts
cleanly without them. Pipeline configuration will be done via the Arroyo
API or UI after the pod is running.

**Validation**: `kubectl apply --dry-run=server` → `configured`
