#!/bin/bash
# ============================================================
# Deploy Arroyo Pipeline — Log Normalization
# ============================================================
# Reads OTLP JSON from Tansu Topic A (otel-logs), extracts
# fields, normalizes raw/multiline logs, writes flat JSON
# to Tansu Topic B (otel-logs-normalized).
#
# Usage:
#   ./deploy-pipeline.sh                    # use port-forward (default)
#   ARROYO_URL=http://arroyo:5115 ./deploy-pipeline.sh  # direct K8s DNS
# ============================================================

set -euo pipefail

ARROYO_URL="${ARROYO_URL:-http://localhost:5115}"

echo "=== Deploying Arroyo pipeline ==="
echo "Arroyo API: ${ARROYO_URL}"

# Wait for Arroyo to be ready
echo "--- Waiting for Arroyo API..."
for i in $(seq 1 30); do
	if curl -sf "${ARROYO_URL}/api/v1/ping" >/dev/null 2>&1; then
		echo "Arroyo ready after ${i}s"
		break
	fi
	if [ "$i" -eq 30 ]; then
		echo "ERROR: Arroyo not ready after 30s"
		exit 1
	fi
	sleep 1
done

PIPELINE_SQL=$(
	cat <<'EOF'
-- ============================================================
-- Arroyo Streaming SQL — Log Normalization
-- ============================================================
-- Source: reads OTLP JSON from Tansu as raw_string
CREATE TABLE otel_logs_source (
    value TEXT
) WITH (
    connector = 'kafka',
    bootstrap_servers = 'tansu-broker.monitoring.svc.cluster.local:9092',
    topic = 'otel-logs',
    type = 'source',
    format = 'raw_string',
    'source.offset' = 'earliest'
);

-- Sink: flat JSON to Tansu Topic B
CREATE TABLE otel_logs_sink (
    timestamp           TIMESTAMP,
    observed_timestamp  TIMESTAMP,
    severity_number     INT,
    severity_text       TEXT,
    body                TEXT,
    k8s_namespace       TEXT,
    k8s_pod             TEXT,
    k8s_container       TEXT,
    k8s_node            TEXT,
    k8s_deployment      TEXT,
    log_format          TEXT,
    processing_state    TEXT,
    cluster_name        TEXT
) WITH (
    connector = 'kafka',
    bootstrap_servers = 'tansu-broker.monitoring.svc.cluster.local:9092',
    topic = 'otel-logs-normalized',
    type = 'sink',
    format = 'json'
);

-- Pipeline: extract from OTLP JSON and normalize
INSERT INTO otel_logs_sink
WITH extracted AS (
    SELECT
        to_timestamp_micros(
            CAST(
                extract_json_string(value, '$.resourceLogs[0].scopeLogs[0].logRecords[0].timeUnixNano')
                AS BIGINT
            ) / 1000
        ) AS timestamp,
        to_timestamp_micros(
            CAST(
                extract_json_string(value, '$.resourceLogs[0].scopeLogs[0].logRecords[0].observedTimeUnixNano')
                AS BIGINT
            ) / 1000
        ) AS observed_timestamp,
        CAST(
            extract_json_string(value, '$.resourceLogs[0].scopeLogs[0].logRecords[0].severityNumber')
            AS INT
        ) AS severity_number,
        extract_json_string(value, '$.resourceLogs[0].scopeLogs[0].logRecords[0].severityText') AS severity_text,
        extract_json_string(value, '$.resourceLogs[0].scopeLogs[0].logRecords[0].body.stringValue') AS body,
        extract_json_string(
            value,
            '$.resourceLogs[0].resource.attributes[?(@.key=="k8s.namespace.name")].value.stringValue'
        ) AS k8s_namespace,
        extract_json_string(
            value,
            '$.resourceLogs[0].resource.attributes[?(@.key=="k8s.pod.name")].value.stringValue'
        ) AS k8s_pod,
        extract_json_string(
            value,
            '$.resourceLogs[0].resource.attributes[?(@.key=="k8s.container.name")].value.stringValue'
        ) AS k8s_container,
        extract_json_string(
            value,
            '$.resourceLogs[0].resource.attributes[?(@.key=="k8s.node.name")].value.stringValue'
        ) AS k8s_node,
        extract_json_string(
            value,
            '$.resourceLogs[0].resource.attributes[?(@.key=="k8s.deployment.name")].value.stringValue'
        ) AS k8s_deployment,
        'homelab' AS cluster_name,
        extract_json_string(
            value,
            '$.resourceLogs[0].scopeLogs[0].logRecords[0].attributes[?(@.key=="log.format")].value.stringValue'
        ) AS log_format
    FROM otel_logs_source
),
with_state AS (
    SELECT
        *,
        CASE
            WHEN log_format IS NULL OR log_format = '' OR log_format = 'raw' THEN 'to_normalize'
            ELSE 'normalized'
        END AS processing_state
    FROM extracted
)
SELECT * FROM with_state;
EOF
)

echo "--- Creating pipeline..."
HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${ARROYO_URL}/api/v1/pipelines" \
	-H "Content-Type: application/json" \
	-d "$(jq -n --arg query "$PIPELINE_SQL" '{name: "otel-logs-normalization", query: $query}')")

HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -1)
HTTP_BODY=$(echo "$HTTP_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
	PIPELINE_ID=$(echo "$HTTP_BODY" | jq -r '.id // empty')
	echo "✅ Pipeline created! ID: ${PIPELINE_ID:-unknown}"
	echo "   View at: ${ARROYO_URL}/pipelines/${PIPELINE_ID}"
else
	echo "❌ Failed to create pipeline (HTTP ${HTTP_CODE})"
	echo "   Response: ${HTTP_BODY}"
	exit 1
fi
