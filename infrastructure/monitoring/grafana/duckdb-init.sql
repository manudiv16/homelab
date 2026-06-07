-- DuckDB InitSQL for Grafana datasource
-- Configured to read Parquet files from Scaleway S3 bucket: local-logs
-- 
-- Structure:
--   s3://local-logs/raw/containers/year=YYYY/month=MM/day=DD/hour=HH/*.parquet
--   s3://local-logs/raw/events/year=YYYY/month=MM/day=DD/hour=HH/*.parquet
--   s3://local-logs/hourly/containers/year=YYYY/month=MM/day=DD/hour=HH/data.parquet  (compacted)
--   s3://local-logs/hourly/events/year=YYYY/month=MM/day=DD/hour=HH/data.parquet      (compacted)
--
-- Schema (containers): time, log, id, namespace, pod, container, node
-- Schema (events):     time, id, namespace, object_name, object_kind, action, reason, message, type

INSTALL httpfs;
LOAD httpfs;
INSTALL aws;
LOAD aws;
SET s3_endpoint='s3.fr-par.scw.cloud';
SET s3_region='fr-par';
SET s3_url_style='vhost';
SET threads=2;
SET http_retries=3;
SET http_retry_wait_ms=500;
SET http_retry_backoff=2;
INSTALL cache_httpfs FROM community;
LOAD cache_httpfs;
SET cache_httpfs_glob_cache_entry_timeout_millisec=60000;
SET s3_access_key_id='SCWE6EQT6ZX1JZ8PASNE';
SET s3_secret_access_key='b821b7ad-e3ed-4138-b7d7-0f576684d329';

-- Container logs macro: reads from raw/ (hourly/ added when compactor runs)
CREATE OR REPLACE MACRO logs(log_group, start_time, end_time) AS TABLE (
    SELECT * REPLACE (time::TIMESTAMPTZ AS time)
    FROM read_parquet(
        's3://local-logs/raw/' || log_group || '/*/*/*/*/*.parquet',
        hive_partitioning = true, union_by_name = true
    )
    WHERE make_timestamp(year::BIGINT, month::BIGINT, day::BIGINT, hour::BIGINT, 0, 0)
            BETWEEN date_trunc('hour', start_time::TIMESTAMPTZ) - INTERVAL 1 HOUR
                AND date_trunc('hour', end_time::TIMESTAMPTZ) + INTERVAL 1 HOUR
      AND time::TIMESTAMPTZ BETWEEN start_time::TIMESTAMPTZ AND end_time::TIMESTAMPTZ
    QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY time::TIMESTAMPTZ DESC) = 1
);

-- Kubernetes events macro
CREATE OR REPLACE MACRO events(start_time, end_time) AS TABLE (
    SELECT * REPLACE (time::TIMESTAMPTZ AS time)
    FROM read_parquet(
        's3://local-logs/raw/events/*/*/*/*/*.parquet',
        hive_partitioning = true, union_by_name = true
    )
    WHERE make_timestamp(year::BIGINT, month::BIGINT, day::BIGINT, hour::BIGINT, 0, 0)
            BETWEEN date_trunc('hour', start_time::TIMESTAMPTZ) - INTERVAL 1 HOUR
                AND date_trunc('hour', end_time::TIMESTAMPTZ) + INTERVAL 1 HOUR
      AND time::TIMESTAMPTZ BETWEEN start_time::TIMESTAMPTZ AND end_time::TIMESTAMPTZ
    QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY time::TIMESTAMPTZ DESC) = 1
);