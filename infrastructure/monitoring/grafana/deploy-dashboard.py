#!/usr/bin/env python3
"""Deploy Grafana Logs Browser dashboard via API.

Usage:
    python3 deploy-dashboard.py
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


# Log level extraction from JSON or CRI-style logs
LEVEL_EXPR = """CASE
  WHEN log LIKE '{%}' THEN COALESCE(NULLIF(json_extract_string(log, '$.level'), ''), 'unknown')
  WHEN log ~ '\\[(\\w+)\\]' THEN COALESCE(NULLIF(regexp_extract(log, '\\[(\\w+)\\]', 1), ''), 'unknown')
  ELSE 'unknown'
END"""

LEVEL_VAR_SQL = f"""SELECT DISTINCT lvl FROM (
  SELECT {LEVEL_EXPR} AS lvl
  FROM read_parquet('s3://local-logs/raw/containers/*/*/*/*/*.parquet', hive_partitioning=true, union_by_name=true)
  WHERE $__timeFilter(time::TIMESTAMPTZ)
) WHERE lvl IS NOT NULL ORDER BY lvl"""

NAMESPACE_VAR_SQL = """SELECT DISTINCT namespace
FROM read_parquet('s3://local-logs/raw/containers/*/*/*/*/*.parquet', hive_partitioning=true, union_by_name=true)
WHERE $__timeFilter(time::TIMESTAMPTZ)
ORDER BY namespace"""

# When All is selected $var = '$__all' (literal), bypass filter.
NS_FILTER = "('$namespace' = '$__all' OR namespace = '$namespace')"
LEVEL_FILTER = f"('$level' = '$__all' OR {LEVEL_EXPR} = '$level')"

LOG_VOLUME_SQL = f"""SELECT
  date_trunc('minute', time::TIMESTAMPTZ) AS time_minute,
  namespace,
  COUNT(*) AS log_count
FROM read_parquet(
  's3://local-logs/raw/containers/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE $__timeFilter(time::TIMESTAMPTZ)
  AND {NS_FILTER}
  AND {LEVEL_FILTER}
GROUP BY time_minute, namespace
ORDER BY time_minute"""

CONTAINER_LOGS_SQL = f"""SELECT
  time::TIMESTAMPTZ AS time,
  log AS line,
  namespace,
  pod,
  container,
  node AS host,
  {LEVEL_EXPR} AS level
FROM read_parquet(
  's3://local-logs/raw/containers/*/*/*/*/*.parquet',
  hive_partitioning = true,
  union_by_name = true
)
WHERE $__timeFilter(time::TIMESTAMPTZ)
  AND {NS_FILTER}
  AND {LEVEL_FILTER}
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
  AND ('$namespace' = '$__all' OR namespace = '$namespace')
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
        "templating": {
            "list": [
                {
                    "name": "namespace",
                    "type": "query",
                    "datasource": {"uid": DUCKDB_UID},
                    "query": NAMESPACE_VAR_SQL,
                    "includeAll": True,
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
    print(f"Dashboard: {result.get('status', 'error')} (v{result.get('version', '?')})")
    print(f"URL: {result.get('url', '')}")
