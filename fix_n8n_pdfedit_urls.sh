#!/usr/bin/env bash
# Reemplaza en los workflows de n8n (Postgres) la URL pública vieja de pdfedit
# por la interna http://pdfedit. Corre psql desde un contenedor efímero en Azure
# (ACI) porque el puerto 5432 no es alcanzable desde la red local.
# Uso: ./fix_n8n_pdfedit_urls.sh            # aplica
#      DRY_RUN=1 ./fix_n8n_pdfedit_urls.sh  # solo muestra
set -euo pipefail

PG_SERVER=n8ndbikvy6n; PG_DB=n8n; PG_USER=n8nadmin
OLD_HOST='pdfedit.thankfulsky-cf68888c.eastus.azurecontainerapps.io'
NEW_URL='http://pdfedit'
ACI_RG=autos; ACI_NAME=n8nfix-$RANDOM; ACI_LOC=westus3

PGPASS=$(az containerapp show -n n8n -g autos \
  --query "properties.template.containers[0].env[?name=='DB_POSTGRESDB_PASSWORD'].value | [0]" -o tsv)

SQL_SHOW="
SELECT '== Workflows que mencionan pdfedit' AS paso;
SELECT id, name, active,
       (nodes::text LIKE '%thankfulsky-cf68888c%') AS url_vieja,
       (nodes::text LIKE '%http://pdfedit%')  AS url_interna
FROM workflow_entity WHERE nodes::text ILIKE '%pdfedit%' ORDER BY name;
SELECT '== URLs exactas' AS paso;
SELECT DISTINCT m[1] FROM workflow_entity,
  regexp_matches(nodes::text, 'https?://[A-Za-z0-9.:_-]*pdfedit[A-Za-z0-9.:_/-]*', 'g') AS m;
SELECT '== Credenciales que mencionan pdfedit (nombre)' AS paso;
SELECT id, name, type FROM credentials_entity WHERE name ILIKE '%pdfedit%';
"
SQL_FIX="
SELECT '== Respaldo en tabla workflow_entity_bak_pdfedit' AS paso;
CREATE TABLE IF NOT EXISTS workflow_entity_bak_pdfedit AS SELECT * FROM workflow_entity;
SELECT '== Aplicando reemplazos' AS paso;
UPDATE workflow_entity
SET nodes = regexp_replace(nodes::text,
      'https?://pdfedit(\\.internal)?\\.thankfulsky-cf68888c\\.eastus\\.azurecontainerapps\\.io(:[0-9]+)?|http://pdfedit:8000',
      '$NEW_URL', 'g')::json,
    \"updatedAt\" = now()
WHERE nodes::text LIKE '%thankfulsky-cf68888c%' OR nodes::text LIKE '%http://pdfedit:8000%';
SELECT '== Verificación (debe quedar vacío)' AS paso;
SELECT id, name FROM workflow_entity WHERE nodes::text LIKE '%thankfulsky-cf68888c%';
"
SQL="$SQL_SHOW"; [ "${DRY_RUN:-0}" = "1" ] || SQL="$SQL_SHOW$SQL_FIX"

echo "==> Ejecutando psql en ACI $ACI_NAME ($ACI_LOC)"
az container create -g $ACI_RG -n $ACI_NAME -l $ACI_LOC --os-type Linux \
  --image postgres:16-alpine --cpu 1 --memory 1 --restart-policy Never \
  --secure-environment-variables PGPASSWORD="$PGPASS" \
  --environment-variables PGHOST=$PG_SERVER.postgres.database.azure.com PGDATABASE=$PG_DB PGUSER=$PG_USER PGSSLMODE=require \
  --command-line "psql -v ON_ERROR_STOP=1 -c \"$(echo "$SQL" | tr '\n' ' ' | sed 's/"/\\"/g')\"" -o none

for i in $(seq 1 40); do
  ST=$(az container show -g $ACI_RG -n $ACI_NAME --query 'instanceView.state' -o tsv 2>/dev/null || echo "")
  [ "$ST" = "Succeeded" ] || [ "$ST" = "Failed" ] || [ "$ST" = "Terminated" ] && break
  sleep 5
done
echo "==> Estado ACI: $ST"
az container logs -g $ACI_RG -n $ACI_NAME
az container delete -g $ACI_RG -n $ACI_NAME --yes -o none

if [ "${DRY_RUN:-0}" != "1" ]; then
  echo "==> Reiniciando n8n para recargar workflows activos"
  REV=$(az containerapp revision list -n n8n -g autos --query '[?properties.active].name | [0]' -o tsv)
  az containerapp revision restart -n n8n -g autos --revision "$REV" -o none
  echo "Listo."
fi
