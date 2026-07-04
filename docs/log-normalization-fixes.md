# Log Normalization Pipeline — Fix Plan

> **Objetivo:** Completar el pipeline OTel → Tansu → Arroyo → Iceberg → Trino  
> **Fecha:** 2026-06-25  
> **Estado previo:** SDD aplicado pero 3 componentes rotos (Arroyo CrashLoop, Lakekeeper sin bootstrap, Trino reiniciando) + OTel Collector con parser CRI que falla en logs no-structured

---

## Resumen de problemas

| Componente | Problema | Impacto |
| ------------ | ---------- | --------- |
| **OTel Collector** | Parser CRI falla en CoreDNS, ArgoCD, etc. → logs no llegan a Kafka | Logs no-structured se pierden |
| **Lakekeeper** | Sin bootstrap → no existe warehouse `logs` | Arroyo y Trino no pueden leer/escribir Iceberg |
| **Lakekeeper secrets** | `pg-url` y `postgres-password` = `changeme` | Postgres no conecta |
| **Arroyo ConfigMap** | `catalog_uri` apunta a `tansu-broker:8181` (puerto erróneo) | Sink Iceberg apunta a destino incorrecto |
| **Arroyo Deployment** | Env var `ICEBERG-CATALOG-URI` no soportada → crash al arrancar | CrashLoopBackOff (120 restarts) |
| **Arroyo image** | `:latest` sin pin | Inestable entre pulls |
| **Trino** | Sin `iceberg.rest-catalog.warehouse` explícito | Fallo al consultar catálogo |

---

## Flujo objetivo

```
OTel Collector (1st pass)
  │  filelog → cri/json/glog/kv parsers
  │  → k8sattributes → transform/normalize-logs
  │  → batch → kafka/tansu exporter (OTLP proto)
  ▼
Tansu (Kafka-compatible broker)
  │  topic: otel-logs, storage S3
  ▼
Arroyo (Streaming SQL, 2nd pass)
  │  Source: Kafka otel-logs (JSON)
  │  Transform: HOP 10s, GROUP BY (ns,pod,container), list_agg(body) → reassembly
  │  Sink: Iceberg (catalog_uri + warehouse + namespace + table)
  ▼
Lakekeeper (Iceberg REST Catalog)
  │  Metadatos en Postgres, archivos Parquet en S3
  ▼
Trino (Distributed SQL query)
  │  Connector iceberg → REST catalog URI → Lakekeeper
  │  Queries sobre s3://logs/iceberg/logs/otel_logs_normalized
```

---

## Paso 1 — OTel Collector: Catch-all parser + normalización raw

**Archivo:** `infrastructure/otel-collector/configmap.yaml`

### 1.1 Añadir parser `raw-fallback` al final de `operators`

Después del último parser existente (`kv-parser`), añadir:

```yaml
        - id: raw-fallback
          type: regex_parser
          regex: '^(?P<log>.*)$'
          parse_from: body
          parse_to: attributes
          on_error: send_quiet
```

**Explicación:** Si ninguna línea de log matchea CRI/JSON/glog/kv, el regex `^(?P<log>.*)$` captura toda la línea como campo `log`. `on_error: send_quiet` asegura que nunca falle.

### 1.2 Añadir statements raw al transform

En `processors.transform/normalize-logs.log_statements`, añadir al final:

```yaml
            - context: log
              statements:
                - set(attributes["log.format"], "raw") where attributes["log.format"] == nil
                - set(severity_number, 9) where severity_number == nil
                - set(severity_text, "INFO") where severity_text == nil
                - set(body, attributes["log"]) where attributes["log"] != nil and body == ""
```

**Explicación:**

- Si ningún parser setó `log.format`, se marca como `raw`
- Sin severity detectada → INFO (9)
- Si el body vino del fallback (`attributes["log"]`), se mueve a `body`

### 1.3 Aplicar

```bash
kubectl apply -f infrastructure/otel-collector/configmap.yaml
# El operator reiniciará los pods del DaemonSet automáticamente
```

### 1.4 Verificar

```bash
kubectl logs -n monitoring -l app.kubernetes.io/component=opentelemetry-collector --tail=50
# Ya no debe aparecer "failed to parse containerd log: regex pattern does not match"

kubectl logs -n monitoring otel-gateway-collector-0 --tail=10
# Debe seguir "Everything is ready. Begin running and processing data."
```

---

## Paso 2 — Lakekeeper: Crear secret con credenciales reales

**Archivo:** `infrastructure/lakekeeper/secret.yaml` (o `kustomize/` equivalente)

### 2.1 Cambiar `pg-url` y `postgres-password`

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: lakekeeper-secrets
  namespace: monitoring
  labels:
    app.kubernetes.io/name: lakekeeper
type: Opaque
stringData:
  # Cambiar a credenciales reales del PostgreSQL de Lakekeeper
  pg-url: "postgresql://lakekeeper:<PASSWORD_REAL>@<PG_HOST>:5432/lakekeeper"
  postgres-password: "<PASSWORD_REAL>"
```

> **Nota:** Si PostgreSQL ya existe y tiene credenciales válidas, usar esas. Si no, crearlas primero en PostgreSQL.

### 2.2 Aplicar

```bash
kubectl apply -f infrastructure/lakekeeper/secret.yaml
kubectl rollout restart deployment lakekeeper -n monitoring
```

### 2.3 Verificar

```bash
kubectl get pods -n monitoring -l app.kubernetes.io/name=lakekeeper
kubectl logs -n monitoring -l app.kubernetes.io/name=lakekeeper --tail=10
# Debe ver "Lakekeeper is now running" sin errores de Postgres
```

---

## Paso 3 — Lakekeeper: Bootstrap (warehouse + namespace)

Se ejecuta **una vez** tras el bootstrap inicial de Lakekeeper.

### 3.1 Port-forward

```bash
kubectl port-forward -n monitoring svc/lakekeeper 8181:8181
# Mantener en otra terminal
```

### 3.2 Crear admin (bootstrap)

```bash
curl -X POST http://localhost:8181/api/v1/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}'
```

> El endpoint puede variar según la versión de Lakekeeper. Si 404, probar:
>
> - `POST http://localhost:8181/bootstrap`
> - `POST http://localhost:8181/catalog/v1/bootstrap`
> - O usar la UI: `http://localhost:8181/ui/`

### 3.3 Crear warehouse

```bash
curl -X POST http://localhost:8181/catalog/v1/warehouses \
  -H "Content-Type: application/json" \
  -d '{"name":"logs","location":"s3://logs/iceberg"}'
```

### 3.4 Crear namespace

```bash
curl -X POST "http://localhost:8181/catalog/v1/namespaces?warehouse=logs" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"logs"}'
```

### 3.5 Verificar

```bash
# Debe devolver config de catálogo sin error 404
curl "http://localhost:8181/catalog/v1/config?warehouse=logs"

# Debe listar el namespace
curl "http://localhost:8181/catalog/v1/namespaces?warehouse=logs"
```

---

## Paso 4 — Arroyo: ConfigMap con catalog_uri correcto

**Archivo:** `infrastructure/arroyo/configmap.yaml`

### 4.1 Corregir sink Iceberg en `pipeline.sql`

En el `CREATE TABLE normalized_logs_sink`:

```sql
-- Write normalized results to Iceberg
CREATE TABLE normalized_logs_sink
WITH (
    connector = 'iceberg',
    catalog_uri = 'http://lakekeeper.monitoring.svc.cluster.local:8181/catalog',  -- CORREGIDO: era tansu-broker:8181
    warehouse = 's3://logs/iceberg',
    namespace = 'logs',
    table_name = 'otel_logs_normalized'
) AS SELECT * FROM normalized_logs;
```

### 4.2 Aplicar

```bash
kubectl apply -f infrastructure/arroyo/configmap.yaml
```

---

## Paso 5 — Arroyo: Deployment sin env var inválida + imagen pinned

**Archivo:** `infrastructure/arroyo/deployment.yaml`

### 5.1 Eliminar env var inválida

El comentario existente en el archivo dice:

```yaml
# No ARROYO__ env vars — unknown fields crash the process.
```

Si hay algún bloque `env:` que incluya `ICEBERG-CATALOG-URI` o similar, **eliminarlo completamente**. Arroyo no lee esa variable.

### 5.2 Pin image tag

```yaml
containers:
  - name: arroyo
    image: ghcr.io/arroyosystems/arroyo:0.10.0  # CORREGIDO: era :latest
```

> **Nota:** Verificar la última versión estable en [GitHub Releases](https://github.com/arroyosystems/arroyo/releases). Si `0.10.0` no existe, usar la última disponible.

### 5.3 Aplicar

```bash
kubectl apply -f infrastructure/arroyo/deployment.yaml
```

### 5.4 Verificar

```bash
kubectl get pods -n monitoring -l app.kubernetes.io/name=arroyo
# Debe estar Running (no CrashLoopBackOff)

kubectl logs -n monitoring -l app.kubernetes.io/name=arroyo --tail=50
# Debe ver la UI/API arrancar sin "Configuration is invalid"
```

---

## Paso 6 — Trino: Warehouse explícito en catálogo Iceberg

**Archivo:** `infrastructure/iceberg-query/trino-catalog-configmap.yaml` → `iceberg.properties`

### 6.1 Añadir `iceberg.rest-catalog.warehouse`

```properties
connector.name=iceberg
iceberg.catalog.type=rest
iceberg.rest-catalog.uri=http://lakekeeper.monitoring.svc.cluster.local:8181/catalog
iceberg.rest-catalog.warehouse=logs          # AÑADIDO
fs.native-s3.enabled=true
s3.endpoint=https://s3.fr-par.scw.cloud
s3.region=fr-par
s3.aws-access-key=${ENV:AWS_ACCESS_KEY_ID}
s3.aws-secret-key=${ENV:AWS_SECRET_ACCESS_KEY}
s3.path-style-access=true
```

### 6.2 Aplicar

```bash
kubectl apply -f infrastructure/iceberg-query/trino-catalog-configmap.yaml
kubectl rollout restart deployment trino -n monitoring
```

### 6.3 Verificar

```bash
kubectl get pods -n monitoring -l app.kubernetes.io/name=trino
kubectl logs -n monitoring -l app.kubernetes.io/name=trino --tail=20
# Debe ver "Server startup completed" y "Added catalog iceberg"
```

---

## Paso 7 — ArgoCD Sync ordenado

```bash
argocd app sync otel-collector      # 1. OTel Collector (nuevo parser raw-fallback)
argocd app sync lakekeeper          # 2. Lakekeeper (secretos)
argocd app sync arroyo              # 3. Arroyo (config + deployment)
argocd app sync iceberg-query       # 4. Trino (warehouse explícito)
```

Verificar estado:

```bash
argocd app get otel-collector
argocd app get lakekeeper
argocd app get arroyo
argocd app get iceberg-query
# Todos deben estar Synced + Healthy
```

---

## Paso 8 — Verificación end-to-end

### 8.1 Logs llegan a Kafka (Tansu)

```bash
kubectl logs -n monitoring tansu-broker-0 --tail=20
# Debe ver actividad en topic otel-logs
```

### 8.2 Arroyo healthy

```bash
kubectl get pods -n monitoring -l app.kubernetes.io/name=arroyo
kubectl logs -n monitoring -l app.kubernetes.io/name=arroyo --tail=20
```

### 8.3 Tabla creada en Lakekeeper

```bash
curl "http://localhost:8181/catalog/v1/namespaces/logs/tables?warehouse=logs"
```

### 8.4 Trino puede consultar

```bash
kubectl exec -n monitoring trino-xxx -- trino --execute \
  "SHOW SCHEMAS FROM iceberg.logs;"

kubectl exec -n monitoring trino-xxx -- trino --execute \
  "SELECT * FROM iceberg.logs.otel_logs_normalized LIMIT 5;"
```

### 8.5 Grafana (si dashboard configurado)

Verificar que los dashboards muestran datos nuevos del pipeline Iceberg.

---

## Checklist de verificación

- [ ] Paso 1: OTel Collector sin errores de parser CRI
- [ ] Paso 1: CoreDNS/ArgoCD logs llegan a Kafka (log.format=raw)
- [ ] Paso 2: Lakekeeper secrets aplicados, deployment restarted
- [ ] Paso 3: Lakekeeper bootstrap ok (warehouse `logs` + namespace `logs` existen)
- [ ] Paso 4: Arroyo ConfigMap con `catalog_uri` a `lakekeeper:8181/catalog`
- [ ] Paso 5: Arroyo Deployment sin env var inválida, imagen pinned
- [ ] Paso 5: Arroyo Running (0 restarts)
- [ ] Paso 6: Trino con `iceberg.rest-catalog.warehouse=logs`
- [ ] Paso 6: Trino Running (0 restarts desde fix)
- [ ] Paso 7: Todos los ArgoCD apps Synced + Healthy
- [ ] Paso 8: Trino puede query `iceberg.logs.otel_logs_normalized`

---

## Troubleshooting

### Arroyo sigue en CrashLoop

```bash
kubectl logs -n monitoring -l app.kubernetes.io/name=arroyo --previous
# Verificar si sigue "Configuration is invalid"
# Si sí: algún env var residual, verificar el deployment completo
```

### Lakekeeper no responde al bootstrap

```bash
kubectl logs -n monitoring -l app.kubernetes.io/name=lakekeeper --tail=50
# Verificar si Postgres conecta correctamente
# Verificar si el puerto 8181 está escuchando
```

### Trino no encuentra tablas

```bash
kubectl exec -n monitoring trino-xxx -- trino --execute "SHOW CATALOGS;"
kubectl exec -n monitoring trino-xxx -- trino --execute "SHOW SCHEMAS FROM iceberg;"
# Si falla: verificar que Lakekeeper tiene warehouse + namespace creados
```

### OTel Collector sigue fallando con CRI

```bash
kubectl logs -n monitoring -l app.kubernetes.io/component=opentelemetry-collector --tail=50
# Verificar que el raw-fallback aparece en la config aplicada
# Si sigue: puede que el ConfigMap no se haya aplicado correctamente
```

---

## Archivos modificados

| Archivo | Pasos | Cambios |
| --------- | ------- | --------- |
| `infrastructure/otel-collector/configmap.yaml` | 1 | +raw-fallback parser, +transform statements |
| `infrastructure/lakekeeper/secret.yaml` | 2 | Credenciales reales |
| `infrastructure/arroyo/configmap.yaml` | 4 | catalog_uri corregido |
| `infrastructure/arroyo/deployment.yaml` | 5 | Quitar env var, pin image |
| `infrastructure/iceberg-query/trino-catalog-configmap.yaml` | 6 | +warehouse=logs |
| `infrastructure/lakekeeper/bootstrap-job.yaml` | (opcional) | ArgoCD PostSync Job |
