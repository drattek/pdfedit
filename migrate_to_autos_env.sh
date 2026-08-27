#!/usr/bin/env bash
# Migra pdfedit (ingress interno) y n8n (ingress externo + n8n.vegusa.com) del
# environment "n8n" (East US, sin VNet) al environment "autos-env" (westus3, VNet VCLOUD).
# Idempotente: se puede re-ejecutar. Al final apaga y borra las apps viejas.
#
# Uso:  ./migrate_to_autos_env.sh            # migra todo
#       KEEP_OLD=1 ./migrate_to_autos_env.sh # migra pero no borra las apps viejas
set -euo pipefail

SUB_ID=d88e33f1-2295-43b7-bb43-a2e20c3e4b60
OLD_RG=n8n
NEW_RG=autos; NEW_ENV=autos-env
DNS_RG=vcorp; DNS_ZONE=vegusa.com; N8N_HOST=n8n.vegusa.com
PDF_ACR=pdfeditacr; N8N_ACR=n8nacr4rtzfo
ST_ACCOUNT=n8nstorage0dn7hpm4dcpq; ST_SHARE=n8n

NEW_VERIFY_ID=$(az containerapp env show -n $NEW_ENV -g $NEW_RG --query properties.customDomainConfiguration.customDomainVerificationId -o tsv)

# Convierte el array env[] de una app a "K=V" / "K=secretref:S"
envs_of() { az containerapp show -n "$1" -g "$2" --query 'properties.template.containers[0].env[]' -o json \
  | python3 -c "import sys,json;[print(e['name']+'='+(('secretref:'+e['secretRef']) if 'secretRef' in e else e.get('value',''))) for e in json.load(sys.stdin)]"; }

############################################################################
echo "==> 1/6 pdfedit en $NEW_ENV (ingress INTERNO)"
if ! az containerapp show -n pdfedit -g $NEW_RG -o none 2>/dev/null; then
  IMAGE=$(az containerapp show -n pdfedit -g $OLD_RG --query 'properties.template.containers[0].image' -o tsv)
  DBPASS=$(az containerapp secret list -n pdfedit -g $OLD_RG --show-values --query "[?name=='dbpass'].value" -o tsv)
  ENVS=(); while IFS= read -r l; do ENVS+=("$l"); done < <(envs_of pdfedit $OLD_RG)
  ENVS+=("AUTOS_API_URL=http://autos-api")
  ACR_U=$(az acr credential show -n $PDF_ACR --query username -o tsv)
  ACR_P=$(az acr credential show -n $PDF_ACR --query 'passwords[0].value' -o tsv)
  az containerapp create -n pdfedit -g $NEW_RG --environment $NEW_ENV \
    --image "$IMAGE" \
    --registry-server "$PDF_ACR.azurecr.io" --registry-username "$ACR_U" --registry-password "$ACR_P" \
    --ingress internal --target-port 8000 \
    --min-replicas 1 --max-replicas 2 --cpu 0.5 --memory 1.0Gi \
    --secrets "dbpass=$DBPASS" --env-vars "${ENVS[@]}" -o none
else
  az containerapp ingress update -n pdfedit -g $NEW_RG --type internal --target-port 8000 -o none
fi
echo "   pdfedit interno: http://pdfedit  (FQDN: $(az containerapp show -n pdfedit -g $NEW_RG --query properties.configuration.ingress.fqdn -o tsv))"

############################################################################
echo "==> 2/6 Azure Files ($ST_ACCOUNT/$ST_SHARE) montado en $NEW_ENV"
if ! az containerapp env storage show -n $NEW_ENV -g $NEW_RG --storage-name n8nfiles -o none 2>/dev/null; then
  ST_KEY=$(az storage account keys list -n $ST_ACCOUNT -g $OLD_RG --query '[0].value' -o tsv)
  az containerapp env storage set -n $NEW_ENV -g $NEW_RG --storage-name n8nfiles \
    --azure-file-account-name $ST_ACCOUNT --azure-file-account-key "$ST_KEY" \
    --azure-file-share-name $ST_SHARE --access-mode ReadWrite -o none
fi

############################################################################
echo "==> 3/6 Apagar n8n viejo (evita dos instancias sobre la misma BD)"
az containerapp update -n n8n -g $OLD_RG --min-replicas 0 --max-replicas 0 -o none 2>/dev/null || true

echo "==> 4/6 n8n en $NEW_ENV (ingress EXTERNO, mismo Postgres, mismo Azure Files)"
if ! az containerapp show -n n8n -g $NEW_RG -o none 2>/dev/null; then
  IMAGE=$(az containerapp show -n n8n -g $OLD_RG --query 'properties.template.containers[0].image' -o tsv)
  ENCKEY=$(az containerapp secret list -n n8n -g $OLD_RG --show-values --query "[?name=='encryptionkey'].value" -o tsv)
  ENVS=(); while IFS= read -r l; do ENVS+=("$l"); done < <(envs_of n8n $OLD_RG)
  ENVS+=("PDFEDIT_URL=http://pdfedit")
  ACR_U=$(az acr credential show -n $N8N_ACR --query username -o tsv)
  ACR_P=$(az acr credential show -n $N8N_ACR --query 'passwords[0].value' -o tsv)
  az containerapp create -n n8n -g $NEW_RG --environment $NEW_ENV \
    --image "$IMAGE" \
    --registry-server "$N8N_ACR.azurecr.io" --registry-username "$ACR_U" --registry-password "$ACR_P" \
    --ingress external --target-port 5678 \
    --min-replicas 1 --max-replicas 1 --cpu 2.0 --memory 4.0Gi \
    --secrets "encryptionkey=$ENCKEY" --env-vars "${ENVS[@]}" -o none
  # Montar el volumen (create no soporta volúmenes; se hace vía YAML)
  TMP=$(mktemp)
  az containerapp show -n n8n -g $NEW_RG -o yaml > "$TMP"
  python3 - "$TMP" <<'PY'
import sys, yaml
p = sys.argv[1]; d = yaml.safe_load(open(p))
t = d['properties']['template']
t['volumes'] = [{'name': 'n8nfiles', 'storageName': 'n8nfiles', 'storageType': 'AzureFile'}]
t['containers'][0]['volumeMounts'] = [{'mountPath': '/home/node/.n8n', 'volumeName': 'n8nfiles'}]
yaml.safe_dump(d, open(p, 'w'))
PY
  az containerapp update -n n8n -g $NEW_RG --yaml "$TMP" -o none
  rm -f "$TMP"
fi
N8N_FQDN=$(az containerapp show -n n8n -g $NEW_RG --query properties.configuration.ingress.fqdn -o tsv)
echo "   n8n nuevo: https://$N8N_FQDN"

############################################################################
echo "==> 5/6 Dominio $N8N_HOST -> $NEW_ENV (DNS en Azure, zona $DNS_ZONE)"
az network dns record-set txt delete -g $DNS_RG -z $DNS_ZONE -n asuid.n8n -y -o none 2>/dev/null || true
az network dns record-set txt add-record -g $DNS_RG -z $DNS_ZONE -n asuid.n8n -v "$NEW_VERIFY_ID" -o none
az network dns record-set cname set-record -g $DNS_RG -z $DNS_ZONE -n n8n -c "$N8N_FQDN" --ttl 300 -o none
echo "   esperando propagación DNS..."
for i in $(seq 1 30); do
  [ "$(dig +short @8.8.8.8 asuid.$N8N_HOST TXT | tr -d '"')" = "$NEW_VERIFY_ID" ] && break; sleep 10
done
if ! az containerapp hostname list -n n8n -g $NEW_RG --query "[?name=='$N8N_HOST']" -o tsv | grep -q .; then
  az containerapp hostname add -n n8n -g $NEW_RG --hostname $N8N_HOST -o none
fi
az containerapp hostname bind -n n8n -g $NEW_RG --hostname $N8N_HOST --environment $NEW_ENV --validation-method CNAME -o none
echo "   certificado administrado emitido y enlazado"

############################################################################
echo "==> 6/6 Permisos del pipeline y limpieza"
SP=$(az ad sp list --display-name pdfedit-github-actions --query '[0].id' -o tsv)
az role assignment create --assignee-object-id "$SP" --assignee-principal-type ServicePrincipal \
  --role Contributor --scope "/subscriptions/$SUB_ID/resourceGroups/$NEW_RG" -o none 2>/dev/null || true

echo "   verificando n8n..."
sleep 30
curl -s -o /dev/null -w "   https://$N8N_HOST/healthz -> HTTP %{http_code}\n" "https://$N8N_HOST/healthz" || true

if [ "${KEEP_OLD:-0}" != "1" ]; then
  echo "   borrando apps viejas en RG $OLD_RG (Postgres, MySQL, storage y ACRs se conservan)"
  az containerapp delete -n pdfedit -g $OLD_RG --yes -o none
  az containerapp delete -n n8n -g $OLD_RG --yes -o none
fi
echo
echo "LISTO."
echo "  n8n:     https://$N8N_HOST"
echo "  pdfedit: http://pdfedit  (solo desde autos-env / VNet VCLOUD)"
echo "  autos:   http://autos-api"
echo "Pendiente en n8n: cambiar en los workflows la URL de pdfedit por http://pdfedit"
