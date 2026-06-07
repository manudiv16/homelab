#!/usr/bin/env python3
"""Deploy Grafana logs dashboard via API."""
import json
import urllib.request
import base64

GRAFANA_URL = "http://192.168.4.80:3000"
GRAFANA_USER = "admin"
GRAFANA_PASS = "admin"
DUCKDB_UID = "P9EB6AA68509EF776"


def grafana_api(method, path, data=None):
    url = f"{GRAFANA_URL}{path}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {base64.b64encode(f'{GRAFANA_USER}:{GRAFANA_PASS}'.encode()).decode()}"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = json.loads(e.read())
        print(f"HTTP {e.code}: {err}")
        return err


# Log volume by namespace (timeseries)
LOG_VOLUME_SQL = """SELECT
  date_trunc('minute', time::TIMESTAMPTZ) AS time_minute,
  namespace,
  COUNT(*) AS log_count
FROM read_parquet(
  's3://local-logs/raw/containers/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE make_timestamp(year::BIGINT, month::BIGINT, day::BIGINT, hour::BIGINT, 0, 0)
        BETWEEN date_trunc('hour', ${__from:date:iso}::TIMESTAMPTZ - INTERVAL '1 hour')
            AND date_trunc('hour', ${__to:date:iso}::TIMESTAMPTZ + INTERVAL '1 hour')
  AND time::TIMESTAMPTZ BETWEEN ${__from:date:iso}::TIMESTAMPTZ AND ${__to:date:iso}::TIMESTAMPTZ
GROUP BY time_minute, namespace
ORDER BY time_minute"""

# Container logs (log panel)
CONTAINER_LOGS_SQL = """SELECT
  time::TIMESTAMPTZ AS time,
  log AS line,
  namespace,
  pod,
  container,
  node AS host
FROM read_parquet(
  's3://local-logs/raw/containers/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE make_timestamp(year::BIGINT, month::BIGINT, day::BIGINT, hour::BIGINT, 0, 0)
        BETWEEN date_trunc('hour', ${__from:date:iso}::TIMESTAMPTZ - INTERVAL '1 hour')
            AND date_trunc('hour', ${__to:date:iso}::TIMESTAMPTZ + INTERVAL '1 hour')
  AND time::TIMESTAMPTZ BETWEEN ${__from:date:iso}::TIMESTAMPTZ AND ${__to:date:iso}::TIMESTAMPTZ
ORDER BY time::TIMESTAMPTZ DESC
LIMIT 5000"""

# Events table
EVENTS_SQL = """SELECT
  time::TIMESTAMPTZ AS time,
  reason,
  message,
  type,
  object_kind,
  namespace,
  object_name
FROM read_parquet(
  's3://local-logs/raw/events/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE make_timestamp(year::BIGINT, month::BIGINT, day::BIGINT, hour::BIGINT, 0, 0)
        BETWEEN date_trunc('hour', ${__from:date:iso}::TIMESTAMPTZ - INTERVAL '1 hour')
            AND date_trunc('hour', ${__to:date:iso}::TIMESTAMPTZ + INTERVAL '1 hour')
  AND time::TIMESTAMPTZ BETWEEN ${__from:date:iso}::TIMESTAMPTZ AND ${__to:date:iso}::TIMESTAMPTZ
ORDER BY time::TIMESTAMPTZ DESC
LIMIT 500"""

dashboard = {
    "dashboard": {
        "uid": "logs-browser",
        "title": "Logs Browser",
        "tags": ["logs", "fluentbit"],
        "timezone": "browser",
        "schemaVersion": 40,
        "refresh": "1m",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            {
                "id": 1,
                "title": "Log Volume by Namespace",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
                "datasource": {"uid": DUCKDB_UID},
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "palette-classic"},
                        "unit": "short",
                        "custom": {"fillOpacity": 30, "spanNulls": True}
                    }
                },
                "options": {
                    "tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "table", "placement": "right"}
                },
                "targets": [{"refId": "A", "rawSql": LOG_VOLUME_SQL}]
            },
            {
                "id": 2,
                "title": "Logs",
                "type": "logs",
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 8},
                "datasource": {"uid": DUCKDB_UID},
                "options": {"showTime": True, "sortOrder": "descending"},
                "targets": [{"refId": "A", "rawSql": CONTAINER_LOGS_SQL}]
            },
            {
                "id": 3,
                "title": "Kubernetes Events",
                "type": "table",
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 18},
                "datasource": {"uid": DUCKDB_UID},
                "fieldConfig": {
                    "defaults": {},
                    "overrides": [
                        {"matcher": {"id": "byName", "options": "message"},
                         "properties": [{"id": "custom.width", "value": 400}]}
                    ]
                },
                "options": {"showHeader": True},
                "targets": [{"refId": "A", "rawSql": EVENTS_SQL}]
            }
        ]
    },
    "overwrite": True
}

result = grafana_api("POST", "/api/dashboards/db", dashboard)
print(f"Dashboard result: {json.dumps(result, indent=2)}")
