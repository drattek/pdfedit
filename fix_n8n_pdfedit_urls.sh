#!/usr/bin/env bash
# Reemplaza en los workflows de n8n (Postgres) la URL pública vieja de pdfedit
# por la interna http://pdfedit. Hace respaldo antes de tocar nada.
# Uso: ./fix_n8n_pdfedit_urls.sh          # aplica
#      DRY_RUN=1 ./fix_n8n_pdfedit_urls.sh  # solo muestra
set -euo pipefail

PG_RG=n8n; PG_SERVER=n8ndbikvy6n; PG_DB=n8n; PG_USER=n8nadmin
OLD_HOST='pdfedit.thankfulsky-cf68888c.eastus.azurecontainerapps.io'
NEW_URL='http://pdfedit'
RULE=tmp-fix-n8n-urls
MYIP=$(curl -s ifconfig.me)

export PGPASSWORD
PGPASSWORD=$(az containerapp show -n n8n -g autos \
  --query "properties.template.containers[0].env[?name=='DB_POSTGRESDB_PASSWORD'].value | [0]" -o tsv)
CONN="host=$PG_SERVER.postgres.database.azure.com dbname=$PG_DB user=$PG_USER sslmode=require"

cleanup() { az postgres flexible-server firewall-rule delete -g $PG_RG -n $PG_SERVER -r $RULE -y -o none 2>/dev/null || true; }
trap cleanup EXIT
echo "==> Abriendo firewall Postgres para $MYIP (temporal)"
az postgres flexible-server firewall-rule create -g $PG_RG -n $PG_SERVER -r $RULE \
  --start-ip-address "$MYIP" --end-ip-address "$MYIP" -o none
sleep 5

echo "==> Respaldo de workflows -> n8n_workflows_backup.sql"
pg_dump "$CONN" -t workflow_entity --data-only --column-inserts > n8n_workflows_backup.sql
echo "   $(wc -l < n8n_workflows_backup.sql) líneas"

echo "==> Workflows que referencian a pdfedit:"
psql "$CONN" -c "
SELECT id, name, active,
       (nodes::text LIKE '%$OLD_HOST%')  AS url_vieja,
       (nodes::text LIKE '%http://pdfedit%') AS url_interna,
       (nodes::text ILIKE '%pdfedit%')   AS menciona
FROM workflow_entity
WHERE nodes::text ILIKE '%pdfedit%'
ORDER BY name;"

echo "==> URLs exactas encontradas:"
psql "$CONN" -Atc "
SELECT DISTINCT m[1] FROM workflow_entity,
  regexp_matches(nodes::text, 'https?://[A-Za-z0-9.:_-]*pdfedit[A-Za-z0-9.:_/-]*', 'g') AS m;"

if [ "${DRY_RUN:-0}" = "1" ]; then echo "(DRY_RUN: no se aplican cambios)"; exit 0; fi

echo "==> Aplicando reemplazos"
psql "$CONN" -c "
UPDATE workflow_entity
SET nodes = replace(replace(replace(nodes::text,
      'https://$OLD_HOST', '$NEW_URL'),
      'http://$OLD_HOST',  '$NEW_URL'),
      'http://pdfedit:8000', '$NEW_URL')::json,
    \"updatedAt\" = now()
WHERE nodes::text LIKE '%$OLD_HOST%' OR nodes::text LIKE '%http://pdfedit:8000%';"

echo "==> Verificación (debe quedar vacío):"
psql "$CONN" -Atc "SELECT id, name FROM workflow_entity WHERE nodes::text LIKE '%$OLD_HOST%';"

echo "==> Reiniciando n8n para que recargue los workflows activos"
az containerapp revision restart -n n8n -g autos \
  --revision "$(az containerapp revision list -n n8n -g autos --query '[?properties.active].name | [0]' -o tsv)" -o none
echo "Listo."
