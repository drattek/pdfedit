
# PDF Edit Service (Overlay) — Sin APIs externas

Servicio FastAPI para **superponer (overlay) texto** en áreas del PDF, pensado para:
- Cambiar **Incoterms** (FCA → DAP) si aplica.
- Reemplazar bloques **Bill To** y **Ship To**.

Usa **ReportLab** para dibujar rectángulos blancos y escribir el nuevo texto, y **PyPDF2** para mezclar la capa con el PDF original.

> ⚠️ Este enfoque utiliza **coordenadas** (x, y, w, h) en puntos **PDF**. Los valores por defecto se ajustan a la plantilla de ejemplo (US Letter). Si tu documento difiere, ajusta las áreas en la llamada.

## Ejecutar local

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t pdf-edit-service .
docker run -p 8000:8000 pdf-edit-service
```

## Endpoints

### `POST /edit_incoterm` → **devuelve PDF**
**Body (JSON):**
```json
{
  "file_b64": "<PDF EN BASE64>",
  "incoterm_change": true,
  "incoterm_text": "DAP",
  "area": { "page": 0, "x": 460, "y": 520, "w": 80, "h": 18 }
}
```

### `POST /edit_billship` → **devuelve PDF**
**Body (JSON):**
```json
{
  "file_b64": "<PDF EN BASE64>",
  "bill_to_text": "MSB LEON, S.A. DE C.V.",
  "ship_to_text": "International Trading & Distribution",
  "bill_to_area": { "page": 0, "x": 72, "y": 560, "w": 220, "h": 80 },
  "ship_to_area": { "page": 0, "x": 300, "y": 560, "w": 250, "h": 80 }
}
```

## Coordenadas
- Origen (0,0) está en la **esquina inferior izquierda**.
- Unidades en **puntos** (1 punto ≈ 1/72 de pulgada). US Letter = 612 × 792 pt.
- Si el texto queda fuera de lugar, ajusta `x`, `y`, `w`, `h` y vuelve a probar.

## Consejos
- Empieza con áreas **más grandes** para cubrir el bloque completo y luego ajusta.
- Si la factura tiene más de una página, `page` empieza en 0 (portada = 0).
- Si necesitas fuentes distintas o tamaño mayor, modifica `font_size` y `leading` en `app.py`.

## Arquitectura en Azure (autos-env)

Desde agosto 2026 el servicio corre en el Container Apps environment **`autos-env`**
(RG `autos`, región **westus3**), integrado a la VNet corporativa **`VCLOUD`** (RG `VCORP`,
subnet `snet-aca-autos` = `10.5.6.0/23`). Es el mismo environment donde viven `n8n` y `autos-api`,
así que los tres se hablan por red interna sin salir a internet.

```
Internet ──HTTPS──> n8n.vegusa.com (n8n, ingress EXTERNO, cert. administrado)
                        │
                        │  http://pdfedit          (ingress INTERNO)
                        ▼
                     pdfedit ──http://autos-api──> autos-api (ingress INTERNO)
                        │                              │
                        │ MySQL (público, TLS)          │ Oracle 1521
                        ▼                              ▼
              pdfeditmysql.mysql.database.azure.com   V55001-1.vegusa.com (10.5.1.11, VNet VCLOUD)
```

| Componente | RG / Environment | Ingress | Cómo se alcanza |
|---|---|---|---|
| **pdfedit** | `autos` / `autos-env` | Interno | `http://pdfedit` (solo desde apps del mismo environment) |
| **autos-api** | `autos` / `autos-env` | Interno | `http://autos-api` (variable `AUTOS_API_URL` en pdfedit) |
| **n8n** | `autos` / `autos-env` | Externo | https://n8n.vegusa.com (DNS y certificado en la zona `vegusa.com` de Azure, RG `vcorp`) |
| **Oracle GlobalDMS** | VM `V55001` (RG `VCORP`) | — | `V55001-1.vegusa.com:1521`, resuelto por los DNS internos de la VNet (10.5.1.8/9) |
| **MySQL pdfedit** | `pdfeditmysql` (RG `n8n`) | — | `MYSQL_HOST` / secreto `dbpass` |
| **Postgres n8n** | `n8ndbikvy6n` (RG `n8n`) | — | mismo servidor de siempre; workflows y credenciales no se movieron |
| **Azure Files n8n** | `n8nstorage0dn7hpm4dcpq/n8n` (RG `n8n`) | — | montado en `/home/node/.n8n` |
| **ACR** | `pdfeditacr` (RG `n8n`) | — | imágenes `pdfedit:<sha>` |

**Importante sobre "interno":** `autos-env` es un environment *externo* con VNet. Las apps con ingress
interno (pdfedit, autos-api) solo son alcanzables desde **otras apps del mismo environment**, no desde
VMs de la VNet ni desde internet. Los FQDN `*.internal.livelyflower-b72499c6.westus3.azurecontainerapps.io`
no sirven fuera del environment; usa los nombres cortos (`http://pdfedit`, `http://autos-api`).

Para probar conectividad desde dentro del environment, lo más fiable es un Container Apps Job efímero
(ver `migrate_to_autos_env.sh` como referencia); `az containerapp exec` requiere una terminal interactiva.

### Historial de migración
- El environment anterior `n8n` (East US, sin VNet) fue eliminado. Las bases de datos, el storage y los
  ACR que estaban en el RG `n8n` se conservan.
- `migrate_to_autos_env.sh`: script (idempotente) que hizo la migración de pdfedit y n8n.
- `fix_n8n_pdfedit_urls.sh`: reemplazó en los workflows de n8n la URL vieja de pdfedit por `http://pdfedit`
  (respaldo en la tabla `workflow_entity_bak_pdfedit` del Postgres de n8n).

## Deploy automático a Azure Container Apps

Cada `git push` a la rama `main` dispara el workflow de GitHub Actions
`.github/workflows/deploy.yml`, que despliega la app **pdfedit** en Azure Container Apps
(grupo de recursos `autos`, environment `autos-env`). También se puede lanzar manualmente desde la pestaña
**Actions → Deploy pdfedit a Azure Container Apps → Run workflow**.

### Qué hace el pipeline

1. **Login a Azure por OIDC** (`azure/login@v2`) — sin contraseñas guardadas; GitHub intercambia
   un token federado por la identidad `pdfedit-github-actions` de Entra ID.
2. **Build de la imagen en ACR**: `az acr build` en el registro `pdfeditacr`, con dos tags:
   el SHA corto del commit (ej. `pdfedit:a30bc673`) y `latest`.
3. **Nueva revisión en Container Apps**: `az containerapp update --image ... --revision-suffix sha-<sha>`.
   La revisión anterior se conserva con 0% de tráfico, por si hay que regresar.
4. **Verificación**: imprime la URL pública y la tabla de revisiones activas.

Cambios que solo tocan archivos `.md`, `deploy_n8n_aca.py`, `Dockerfile.n8n` o `n8n_outlook/`
**no** disparan el deploy.

### Configuración necesaria (ya hecha)

| Dónde | Qué |
|---|---|
| Entra ID | App registration `pdfedit-github-actions` con rol **Contributor** sobre los RG `autos` (app) y `n8n` (ACR) |
| Entra ID | Credencial federada: issuer `token.actions.githubusercontent.com`, subject `repo:drattek/pdfedit:ref:refs/heads/main` |
| GitHub Secrets | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` |

Si se cambia el nombre del repo o de la rama, hay que actualizar el `subject` de la credencial federada.

### Seguir el deploy

```bash
gh run list -L 5          # últimos runs
gh run watch              # seguir el run en curso
az containerapp revision list -n pdfedit -g autos -o table
```

### Regresar a una revisión anterior

```bash
az containerapp ingress traffic set -n pdfedit -g autos \
  --revision-weight pdfedit--sha-<sha-anterior>=100
```

### Cambios de infraestructura

El pipeline solo construye la imagen y publica una revisión. Para crear/rotar secretos,
cambiar CPU/memoria, inicializar la base de datos, etc., sigue usándose el script
`deploy_pdfedit_aca.py` de forma manual (`python deploy_pdfedit_aca.py --help`).
