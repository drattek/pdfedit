from fastapi import FastAPI, Response, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from io import BytesIO
import base64
import os
import time
from datetime import datetime, timedelta
import pdfplumber
from playwright.sync_api import sync_playwright
from reportlab.pdfgen import canvas
from reportlab.lib.colors import white, black
from reportlab.pdfbase.pdfmetrics import stringWidth
from PyPDF2 import PdfReader, PdfWriter

# --- CONTROL DE VERSIONES ---
VERSION = "1.26"
print(f"\n{'='*40}")
print(f" INICIANDO SERVICIO VEGUSA - VERSIÓN: {VERSION}")
print(f" MODO: Producción n8n (Integración Completa)")
print(f" FIX: Overlay con Wrap de Texto (Basado en Overlay OK)")
print(f"{'='*40}\n")

# Inicialización de la aplicación FastAPI
app = FastAPI(title=f"PDF Edit & Doosan Service v{VERSION} — Vegusa Enterprise")

# ---------------------------------------------------------
# MODELOS DE DATOS (Pydantic)
# ---------------------------------------------------------

class ScrapeRequest(BaseModel):
    user: str
    password: str

class DownloadRequest(BaseModel):
    user: str
    password: str
    shipment_ids: List[str]

class Rect(BaseModel):
    page: int = Field(0, description="0-based page index")
    x: float
    y: float
    w: float
    h: float

class CoordinateRequest(BaseModel):
    file_b64: str
    target_text: str

class IncotermReq(BaseModel):
    file_b64: str
    incoterm_change: bool = False
    incoterm_text: str = "DAP"
    area: Optional[Rect] = None
    font_size: Optional[float] = 9.0
    leading: Optional[float] = 11.0
    debug_outline: Optional[bool] = False

class BillShipReq(BaseModel):
    file_b64: str
    bill_to_text: Optional[str] = None
    ship_to_text: Optional[str] = None
    bill_to_area: Optional[Rect] = None
    ship_to_area: Optional[Rect] = None
    font_size: Optional[float] = 9.0
    leading: Optional[float] = 11.0
    debug_outline: Optional[bool] = False

class CustomTextOp(BaseModel):
    text: str
    area: Rect
    font_name: Optional[str] = "Helvetica"
    font_size: Optional[float] = 9.0
    leading: Optional[float] = 11.0
    debug_outline: Optional[bool] = False

class CustomBatchReq(BaseModel):
    file_b64: str
    ops: List[CustomTextOp]

class CutRangeReq(BaseModel):
    file_b64: str
    start_page: int
    final_page: int

class CustomPagesReq(BaseModel):
    file_b64: str
    pages: List[int]

# ---------------------------------------------------------
# UTILIDADES INTERNAS PDF (Lógica de "Overlay OK")
# ---------------------------------------------------------

def _load_pdf_from_b64(file_b64: str) -> PdfReader:
    raw = base64.b64decode(file_b64)
    return PdfReader(BytesIO(raw))

def _export(writer: PdfWriter) -> bytes:
    out = BytesIO()
    writer.write(out)
    return out.getvalue()

def _make_overlay(page_width: float, page_height: float, draw_ops):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))
    draw_ops(c)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf

def _overlay_rect_with_text(
    reader: PdfReader,
    writer: PdfWriter,
    rect: Rect,
    text: str,
    font_name: str = "Helvetica",
    font_size: float = 9.0,
    leading: float = 11.0,
    debug_outline: bool = False
):
    page_index = max(0, min(rect.page, len(reader.pages) - 1))
    page = reader.pages[page_index]
    media = page.mediabox
    pw, ph = float(media.width), float(media.height)

    def draw_ops(c):
        if debug_outline:
            c.setStrokeColorRGB(1, 0, 0)
            c.setLineWidth(1.2)
            c.rect(rect.x, rect.y, rect.w, rect.h, fill=False, stroke=True)
            return
        # Tapar con recuadro blanco
        c.setFillColor(white)
        c.rect(rect.x, rect.y, rect.w, rect.h, fill=True, stroke=False)
        # Escribir texto negro
        c.setFillColor(black)
        c.setFont(font_name, font_size)
        x, y = rect.x + 2, rect.y + rect.h - leading
        max_w = rect.w - 4
        
        for line in (text or '').split("\n"):
            words = line.split(" ")
            cur = ""
            for w in words:
                test = (cur + " " + w).strip() if cur else w
                if stringWidth(test, font_name, font_size) <= max_w:
                    cur = test
                else:
                    c.drawString(x, y, cur)
                    y -= leading
                    cur = w
                    if y < rect.y + 2: return
            if y >= rect.y + 2:
                c.drawString(x, y, cur)
                y -= leading

    overlay_reader = PdfReader(_make_overlay(pw, ph, draw_ops))
    page.merge_page(overlay_reader.pages[0])
    for i, p in enumerate(reader.pages):
        writer.add_page(page if i == page_index else p)

# ---------------------------------------------------------
# UTILIDADES DOOSAN (Navegación Playwright)
# ---------------------------------------------------------

class AuthException(Exception):
    pass

def find_frame_with_selector(page, selector):
    for f in page.frames:
        try:
            if f.locator(selector).count() > 0: return f
        except: continue
    return None

def _doosan_navigate_to_results(context, user, password, f_start, f_end):
    page = context.new_page()
    page.set_default_timeout(90000)
    
    print(f">>> [{VERSION}] Accediendo a Bobcat Login...")
    page.goto('https://dealer.bobcat.com/', wait_until="networkidle", timeout=120000)
    page.wait_for_selector('input[name="identifier"]', timeout=60000)
    page.fill('input[name="identifier"]', user)
    pass_field = page.locator('input[name="credentials.passcode"]')
    pass_field.fill(password)
    time.sleep(1)
    pass_field.press("Enter")
    
    time.sleep(3)
    error_selector = ".infobox-error, .okta-form-infobox-error, .o-form-has-errors"
    if page.locator(error_selector).first.is_visible():
        raise AuthException("usuario o contraseña erróneos")

    page.wait_for_load_state('networkidle', timeout=60000)
    page.wait_for_selector('a:has-text("Doobiz")', timeout=45000)
    with context.expect_page(timeout=90000) as new_page_info:
        page.click('a:has-text("Doobiz")', force=True)
    doobiz_page = new_page_info.value
    doobiz_page.wait_for_load_state('networkidle', timeout=90000)
    
    doobiz_page.evaluate("tlnMoveMenu('ROLES://portal_content/cbt/common/roles/parts/com.di.cbt.cbt_parts_dealer/parts_2/status/shipment_status',0,'width=500,height=750','');")
    time.sleep(15) 
    
    content_frame = None
    for _ in range(15):
        content_frame = doobiz_page.frame(name="isolatedWorkArea")
        if content_frame:
            try:
                if content_frame.locator("#fromPeriod").count() > 0: break
            except: pass
        time.sleep(1)

    if not content_frame:
        content_frame = find_frame_with_selector(doobiz_page, '#fromPeriod')

    if not content_frame:
        raise Exception("No se localizó el frame de trabajo")

    content_frame.locator('#fromPeriod').wait_for(state="visible", timeout=60000)
    content_frame.locator('#fromPeriod').fill(f_start)
    content_frame.locator('#toPeriod').fill(f_end)
    content_frame.locator('button:has-text("Search")').click()
    
    time.sleep(25)
    results_frame = find_frame_with_selector(doobiz_page, 'input[type="radio"]') or content_frame
    return doobiz_page, content_frame, results_frame

# ---------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------

@app.get("/")
def health_check():
    return {"status": "ok", "version": VERSION, "service": "Vegusa PDF & Doosan"}

@app.post("/buscaInvoice")
async def busca_invoice(req: ScrapeRequest):
    today_dt = datetime.now()
    yesterday_dt = today_dt - timedelta(days=1)
    f_start, f_end = yesterday_dt.strftime("%Y.%m.%d"), today_dt.strftime("%Y.%m.%d")
    shipments = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1920, 'height': 1080})
        try:
            _, _, res_frame = _doosan_navigate_to_results(context, req.user, req.password, f_start, f_end)
            rows = res_frame.locator('tr').filter(has=res_frame.locator('input[type="radio"]')).all()
            for row in rows:
                cells = row.locator('td').all()
                if len(cells) > 1:
                    val = cells[1].inner_text().strip().lstrip('0')
                    if val and val != "...": shipments.append(val)
            return {"status": "success", "version": VERSION, "shipments": list(set(shipments))}
        except AuthException as ae:
            return {"status": "auth_error", "user": req.user, "message": str(ae)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        finally: browser.close()

@app.post("/descargaInvoice")
async def descarga_invoice(req: DownloadRequest):
    today_dt = datetime.now()
    f_start, f_end = (today_dt - timedelta(days=7)).strftime("%Y.%m.%d"), today_dt.strftime("%Y.%m.%d")
    files = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1920, 'height': 1080})
        try:
            db_page, c_frame, res_frame = _doosan_navigate_to_results(context, req.user, req.password, f_start, f_end)
            rows = res_frame.locator('tr').filter(has=res_frame.locator('input[type="radio"]')).all()
            for row in rows:
                cells = row.locator('td').all()
                if len(cells) > 1:
                    ship_id = cells[1].inner_text().strip().lstrip('0')
                    if ship_id in req.shipment_ids:
                        cells[0].click(force=True)
                        time.sleep(2)
                        with db_page.expect_download(timeout=90000) as download_info:
                            c_frame.locator('button:has-text("Commercial Invoice")').click(force=True)
                        download = download_info.value
                        with open(download.path(), "rb") as f:
                            b64 = base64.b64encode(f.read()).decode('utf-8')
                        files.append({"shipment_no": ship_id, "filename": f"{ship_id.zfill(10)}.pdf", "base64": b64})
            return {"status": "success", "files": files}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        finally: browser.close()

@app.post("/find_text_coords")
async def find_text_coords(req: CoordinateRequest):
    try:
        pdf_bytes = base64.b64decode(req.file_b64)
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            page_index = len(pdf.pages) - 1
            page = pdf.pages[page_index]
            ph = float(page.height)
            target = req.target_text.upper()
            for word in page.extract_words():
                if target in word['text'].upper():
                    y_coords = ph - float(word['top']) - (float(word['bottom']) - float(word['top']))
                    return {"x": word['x0'], "y": y_coords, "w": 75, "h": 12, "page": page_index, "text_found": word['text']}
        raise HTTPException(status_code=404, detail="Texto no encontrado")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/edit_incoterm", response_class=Response)
async def edit_incoterm(req: IncotermReq):
    reader = _load_pdf_from_b64(req.file_b64)
    writer = PdfWriter()
    area = req.area or Rect(page=0, x=460, y=520, w=80, h=18)
    if req.incoterm_change:
        _overlay_rect_with_text(reader, writer, area, req.incoterm_text, font_size=req.font_size, leading=req.leading, debug_outline=req.debug_outline)
    else:
        for p in reader.pages: writer.add_page(p)
    return Response(content=_export(writer), media_type="application/pdf")

@app.post("/edit_billship", response_class=Response)
async def edit_billship(req: BillShipReq):
    reader = _load_pdf_from_b64(req.file_b64)
    bill_area = req.bill_to_area or Rect(page=0, x=72, y=560, w=220, h=80)
    ship_area = req.ship_to_area or Rect(page=0, x=300, y=560, w=250, h=80)
    writer = PdfWriter()
    if req.bill_to_text:
        _overlay_rect_with_text(reader, writer, bill_area, req.bill_to_text, font_size=req.font_size, leading=req.leading, debug_outline=req.debug_outline)
        reader = PdfReader(BytesIO(_export(writer)))
        writer = PdfWriter()
    else:
        for p in reader.pages: writer.add_page(p)
        reader = PdfReader(BytesIO(_export(writer)))
        writer = PdfWriter()
    if req.ship_to_text:
        _overlay_rect_with_text(reader, writer, ship_area, req.ship_to_text, font_size=req.font_size, leading=req.leading, debug_outline=req.debug_outline)
    else:
        for p in reader.pages: writer.add_page(p)
    return Response(content=_export(writer), media_type="application/pdf")

@app.post("/overlay_text_batch", response_class=Response)
async def overlay_text_batch(req: CustomBatchReq):
    """Lógica de Overlay OK integrada (Multi-pass con wrap de texto)."""
    reader = _load_pdf_from_b64(req.file_b64)
    if not req.ops:
        w = PdfWriter()
        for p in reader.pages: w.add_page(p)
        return Response(content=_export(w), media_type="application/pdf")
    for op in req.ops:
        w = PdfWriter()
        _overlay_rect_with_text(reader, w, op.area, op.text, font_name=(op.font_name or "Helvetica"), font_size=op.font_size or 9.0, leading=op.leading or 11.0, debug_outline=bool(op.debug_outline))
        reader = PdfReader(BytesIO(_export(w)))
    final_writer = PdfWriter()
    for p in reader.pages: final_writer.add_page(p)
    return Response(content=_export(final_writer), media_type="application/pdf")

@app.post("/cut_range", response_class=Response)
async def cut_range(req: CutRangeReq):
    reader = _load_pdf_from_b64(req.file_b64)
    writer = PdfWriter()
    total = len(reader.pages)
    start = max(0, min(req.start_page, total - 1))
    end = max(start, min(req.final_page, total - 1))
    for i in range(start, end + 1):
        writer.add_page(reader.pages[i])
    return Response(content=_export(writer), media_type="application/pdf")

@app.post("/extract_custom_pages", response_class=Response)
async def extract_custom_pages(req: CustomPagesReq):
    reader = _load_pdf_from_b64(req.file_b64)
    writer = PdfWriter()
    total = len(reader.pages)
    for p_num in req.pages:
        if 0 <= p_num < total:
            writer.add_page(reader.pages[p_num])
    return Response(content=_export(writer), media_type="application/pdf")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)