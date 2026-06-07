#!/usr/bin/env python3
"""Update dashboard to use empty string sentinel instead of $__all."""
import json
import urllib.request
import base64
import time as t

GRAFANA_URL = "http://192.168.4.80:3000"
DUCKDB_UID = "P9EB6AA68509EF776"
auth = base64.b64encode(b"admin:admin").decode()

# Get current dashboard
req = urllib.request.Request(f"{GRAFANA_URL}/api/dashboards/uid/logs-browser")
req.add_header("Authorization", f"Basic {auth}")
with urllib.request.urlopen(req, timeout=10) as resp:
    dash = json.loads(resp.read())

db = dash['dashboard']

# 1. Set allValue to "" for variables
for v in db.get('templating', {}).get('list', []):
    v['allValue'] = ""

# 2. Replace '$__all' with '' in all panel SQLs
for p in db.get('panels', []):
    sql = p['targets'][0]['rawSql']
    p['targets'][0]['rawSql'] = sql.replace("'$__all'", "''")

# 3. Deploy
payload = {"dashboard": db, "overwrite": True}
body = json.dumps(payload).encode()
req = urllib.request.Request(f"{GRAFANA_URL}/api/dashboards/db", data=body, headers={
    "Content-Type": "application/json", "Authorization": f"Basic {auth}"
}, method="POST")
with urllib.request.urlopen(req, timeout=30) as resp:
    result = json.loads(resp.read())
print("Dashboard: {} (v{})".format(result.get('status', 'error'), result.get('version', '?')))

# 4. Test with empty string substitution (simulating All selected)
panel_sql = db['panels'][1]['targets'][0]['rawSql']
idx = panel_sql.find("AND (")
print("\nFilter pattern: ...{}...".format(panel_sql[idx:idx+80]))
print()

now_ms = int(t.time() * 1000)
from_ms = now_ms - 10800000

for test_name, ns_val, lvl_val in [
    ("All", "", ""),
    ("level=error", "", "error"),
    ("ns=argocd", "argocd", ""),
    ("ns=argocd level=error", "argocd", "error"),
]:
    test_sql = panel_sql.replace('$namespace', ns_val).replace('$level', lvl_val)
    q = {
        "queries": [{"refId": "A", "datasource": {"uid": DUCKDB_UID}, "rawSql": test_sql}],
        "from": str(from_ms), "to": str(now_ms)
    }
    body = json.dumps(q).encode()
    req = urllib.request.Request("{}/api/ds/query".format(GRAFANA_URL), data=body, headers={
        "Content-Type": "application/json", "Authorization": "Basic {}".format(auth)
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        result = json.loads(e.read())

    r = result.get("results", {}).get("A", {})
    if 'error' in r:
        print("ERROR {}: {}".format(test_name, r['error'][:200]))
    else:
        frames = r.get("frames", [])
        for frame in frames:
            data = frame.get("data", {}).get("values", [])
            if data and data[0]:
                print("OK {}: {} rows".format(test_name, len(data[0])))
            else:
                print("WARN {}: 0 rows".format(test_name))
