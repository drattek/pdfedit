
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
