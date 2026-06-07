#!/usr/bin/env python3
"""Deploy Grafana Logs Browser dashboard via API."""
import json
import urllib.request
import base64

GRAFANA_URL = "http://192.168.4.80:3000"
GRAFANA_USER = "admin"
GRAFANA_PASS = "admin"
DUCKDB_UID = "P9EB6AA68509EF776"


def grafana_api(method, path, data=None):
    url = "{}{}".format(GRAFANA_URL, path)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Basic {}".format(
            base64.b64encode("{}:{}".format(GRAFANA_USER, GRAFANA_PASS).encode()).decode()
        )
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# Log level extraction from JSON or CRI-style logs
# JSON: {"level":"info"} -> json_extract_string
# CRI:  [2026/06/07] [error] [upstream] -> regexp_extract
LEVEL_EXPR = """CASE
  WHEN log LIKE '{%}' THEN COALESCE(NULLIF(json_extract_string(log, '$.level'), ''), 'unknown')
  WHEN log ~ '\\[(\\w+)\\]' THEN COALESCE(NULLIF(regexp_extract(log, '\\[(\\w+)\\]', 1), ''), 'unknown')
  ELSE 'unknown'
END"""

LEVEL_VAR_SQL = """SELECT DISTINCT lvl FROM (
  SELECT {}
  FROM read_parquet(
    's3://local-logs/raw/containers/*/*/*/*/*.parquet',
    hive_partitioning = true,
    union_by_name = true
  )
  WHERE $__timeFilter(time::TIMESTAMPTZ)
) WHERE lvl IS NOT NULL ORDER BY lvl""".format(LEVEL_EXPR + " AS lvl")

NAMESPACE_VAR_SQL = """SELECT DISTINCT namespace
FROM read_parquet(
  's3://local-logs/raw/containers/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE $__timeFilter(time::TIMESTAMPTZ)
ORDER BY namespace"""

# Filter pattern: empty string = All (bypass), specific value = filter
NS_FILTER = "('$namespace' = '' OR namespace = '$namespace')"
LEVEL_FILTER = "('$level' = '' OR {} = '$level')".format(LEVEL_EXPR)

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
  AND {}
  AND {}
GROUP BY time_minute, namespace
ORDER BY time_minute""".format(NS_FILTER, LEVEL_FILTER)

CONTAINER_LOGS_SQL = """SELECT
  time::TIMESTAMPTZ AS time,
  log AS line,
  namespace,
  pod,
  container,
  node AS host,
  {} AS level
FROM read_parquet(
  's3://local-logs/raw/containers/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE $__timeFilter(time::TIMESTAMPTZ)
  AND {}
  AND {}
ORDER BY time::TIMESTAMPTZ DESC
LIMIT 5000""".format(LEVEL_EXPR, NS_FILTER, LEVEL_FILTER)

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
  AND {}
ORDER BY time::TIMESTAMPTZ DESC
LIMIT 500""".format(NS_FILTER)

DASHBOARD = {
    "dashboard": {
        "uid": "logs-browser",
        "title": "Logs Browser",
        "tags": ["logs", "fluentbit"],
        "timezone": "browser",
        "schemaVersion": 40,
        "refresh": "1m",
        "time": {"from": "now-3h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "namespace",
                    "type": "query",
                    "datasource": {"uid": DUCKDB_UID},
                    "query": NAMESPACE_VAR_SQL,
                    "includeAll": True,
                    "allValue": "",
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                    "sort": 1
                },
                {
                    "name": "level",
                    "type": "query",
                    "datasource": {"uid": DUCKDB_UID},
                    "query": LEVEL_VAR_SQL,
                    "includeAll": True,
                    "allValue": "",
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                    "sort": 1
                }
            ]
        },
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
    print("Dashboard: {} (v{})".format(result.get('status', 'error'), result.get('version', '?')))
    print("URL: {}".format(result.get('url', '')))
