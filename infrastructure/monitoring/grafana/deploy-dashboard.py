#!/usr/bin/env python3
"""Deploy Grafana Logs Browser dashboard via API.

Usage:
    python3 deploy-dashboard.py

Requires:
    - Grafana running at http://192.168.4.80:3000
    - admin:admin credentials
    - DuckDB datasource with UID P9EB6AA68509EF776
"""
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


LOG_VOLUME_SQL = """SELECT
  date_trunc('minute', time::TIMESTAMPTZ) AS time_minute,
  namespace,
  COUNT(*) AS log_count
FROM read_parquet(
  's3://local-logs/raw/containers/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE $__timeFilter(time::TIMESTAMPTZ)
GROUP BY time_minute, namespace
ORDER BY time_minute"""

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
WHERE $__timeFilter(time::TIMESTAMPTZ)
ORDER BY time::TIMESTAMPTZ DESC
LIMIT 5000"""

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
WHERE $__timeFilter(time::TIMESTAMPTZ)
ORDER BY time::TIMESTAMPTZ DESC
LIMIT 500"""

DASHBOARD = {
    "dashboard": {
        "uid": "logs-browser",
        "title": "Logs Browser",
        "tags": ["logs", "fluentbit"],
        "timezone": "browser",
        "schemaVersion": 40,
        "refresh": "1m",
        "time": {"from": "now-3h", "to": "now"},
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


if __name__ == "__main__":
    result = grafana_api("POST", "/api/dashboards/db", DASHBOARD)
    print(f"Dashboard: {result.get('status', 'error')} (v{result.get('version', '?')})")
    print(f"URL: {result.get('url', '')}")
