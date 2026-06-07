#!/usr/bin/env python3
"""Test all Grafana dashboard panel queries."""
import json, urllib.request, base64, time as t

GRAFANA_URL = "http://192.168.4.80:3000"
DUCKDB_UID = "P9EB6AA68509EF776"
auth = base64.b64encode(b"admin:admin").decode()

# Get dashboard from API
req = urllib.request.Request(f"{GRAFANA_URL}/api/dashboards/uid/logs-browser")
req.add_header("Authorization", f"Basic {auth}")
with urllib.request.urlopen(req, timeout=10) as resp:
    dash = json.loads(resp.read())

db = dash['dashboard']
panels = db.get('panels', [])
templating = db.get('templating', {}).get('list', [])

now_ms = int(t.time() * 1000)
from_ms = now_ms - 10800000

def run_query(sql, desc):
    q = {
        "queries": [{"refId": "A", "datasource": {"uid": DUCKDB_UID}, "rawSql": sql}],
        "from": str(from_ms),
        "to": str(now_ms)
    }
    body = json.dumps(q).encode()
    req = urllib.request.Request(f"{GRAFANA_URL}/api/ds/query", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Basic {auth}"
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        result = json.loads(e.read())

    r = result.get("results", {}).get("A", {})
    if 'error' in r:
        print(f"❌ {desc}: {r['error'][:300]}")
        sql_meta = r.get('frames', [{}])[0].get('schema', {}).get('meta', {}).get('executedQueryString', '')
        if sql_meta:
            print(f"   SQL ({len(sql_meta)}c): {sql_meta[:250]}")
        return False
    else:
        frames = r.get("frames", [])
        for frame in frames:
            data = frame.get("data", {}).get("values", [])
            if data and data[0]:
                print(f"✅ {desc}: {len(data[0])} rows")
            else:
                print(f"⚠️ {desc}: 0 rows")
        return True

print(f"Dashboard v{db.get('version')}")
print(f"Template variables: {[v['name'] for v in templating]}")

print("\n=== Testing Variable Queries ===")
for v in templating:
    sql = v['query']
    run_query(sql, f"var '{v['name']}'")

print("\n=== Testing Panel Queries ===")
for i, p in enumerate(panels):
    sql = p['targets'][0]['rawSql']
    run_query(sql, f"panel '{p['title']}' ({p['type']})")
