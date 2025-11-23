import os
import re
import tempfile
import shutil
from fastapi import FastAPI, UploadFile, File
import fitz   # PyMuPDF
import easyocr
import dateparser

# ----------------------------------------------------------
# APP INIT
# ----------------------------------------------------------
app = FastAPI(title="Invoice OCR API - PyMuPDF + EasyOCR")

# Load EasyOCR (CPU only)
reader = easyocr.Reader(["en"], gpu=False)

# ----------------------------------------------------------
# Helpers
# ----------------------------------------------------------
def find_date_in_window(lines, start_idx, window=6):
    """Find a date in nearby lines."""
    for i in range(start_idx + 1, min(start_idx + window, len(lines))):
        dt = dateparser.parse(lines[i])
        if dt:
            return dt.strftime("%d %b %Y")
    return None


def extract_supplier_address(lines):
    """Australian address extractor."""
    address = []
    capturing = False

    START = ["level", "suite", "elizabeth"]
    STREET = ["st", "street", "rd", "road", "ave", "avenue"]
    CITY = ["melbourne", "sydney", "brisbane", "perth", "adelaide", "hobart"]
    STOP = ["customer", "payment", "invoice", "unit price", "quantity", "description"]

    for line in lines:
        low = line.lower()

        if not capturing:
            if any(s in low for s in START):
                capturing = True
            else:
                continue

        if any(s in low for s in STOP):
            break

        if (
            any(s in low for s in START) or
            any(s in low for s in STREET) or
            any(s in low for s in CITY) or
            "australia" in low
        ):
            address.append(line)

        if "australia" in low:
            break

    return ", ".join(address).strip()


def extract_abn(text):
    """Extract ABN number."""
    m = re.search(r"ABN[\s:]*([\d ]{11,20})", text, re.IGNORECASE)
    if m:
        return m.group(1).replace(" ", "")
    return None


def extract_items(lines):
    """Safe item extractor supporting simple line items."""
    cleaned = [x.strip() for x in lines if x.strip()]
    items = []
    bucket = []

    numeric = re.compile(r"^\d+(\.\d{1,2})?$")
    money = re.compile(r"^\d+\.\d{2}$")
    gstp = re.compile(r"^\d+%$")

    after_header = False

    for line in cleaned:
        low = line.lower()
        if "unit price" in low or ("description" in low and "quantity" in low):
            after_header = True
            continue

        if after_header:
            if "customer" in low:
                break
            bucket.append(line)

    group = []
    for line in bucket:
        group.append(line)
        nums = [x for x in group if numeric.match(x) or money.match(x) or gstp.match(x)]

        if len(nums) >= 4:
            qty = nums[0]
            unit = nums[1]
            gst = nums[2] if gstp.match(nums[2]) else None
            total = nums[3]

            desc = " ".join([x for x in group if x not in nums and "tixperts-" not in x.lower()]).strip()

            items.append({
                "description": desc,
                "quantity": qty,
                "unit_price": unit,
                "gst_percent": gst,
                "line_total": total
            })
            group = []

    return items


def extract_invoice_fields(lines):
    """Extract structured fields from OCR lines."""
    safe = [str(x).strip() for x in lines if str(x).strip()]
    full = " ".join(safe)

    data = {
        "invoice_details": {},
        "supplier": {},
        "customer": {},
        "items": [],
        "totals": {},
        "payment_terms": {}
    }

    # ---- INVOICE NUMBER ----
    inv = re.findall(r"[A-Z]{2}\.\d{3}-\d{2}\.INV-\d{4}", full)
    data["invoice_details"]["invoice_number"] = inv[0] if inv else None

    # ---- INVOICE DATE ----
    for idx, line in enumerate(safe):
        if "invoice date" in line.lower():
            data["invoice_details"]["invoice_date"] = find_date_in_window(safe, idx)
            break

    if not data["invoice_details"].get("invoice_date"):
        for t in safe:
            dt = dateparser.parse(t)
            if dt:
                data["invoice_details"]["invoice_date"] = dt.strftime("%d %b %Y")
                break

    # ---- DUE DATE ----
    dd = re.search(r"Due Date[:\s]+(\d{1,2} \w+ \d{4})", full)
    data["invoice_details"]["due_date"] = dd.group(1) if dd else None

    # ---- SUPPLIER ----
    supplier = None
    for t in safe:
        if "pty" in t.lower():
            supplier = t.replace("TIA", "T/A")
            break

    data["supplier"]["name"] = supplier
    data["supplier"]["address"] = extract_supplier_address(safe)
    data["supplier"]["abn"] = extract_abn(full)

    # ---- CUSTOMER ----
    for idx, line in enumerate(safe):
        if "customer" in line.lower() and idx + 1 < len(safe):
            data["customer"]["name"] = safe[idx + 1]
            break

    # ---- ITEMS ----
    data["items"] = extract_items(safe)

    # ---- GST AMOUNT ----
    gst_amount = None
    m = re.search(r"INCLUDES GST[^\d]*([\d]+\.[\d]+)", full, re.IGNORECASE)
    if m:
        gst_amount = float(m.group(1))

    # ---- TOTAL ----
    floats = [float(x) for x in re.findall(r"\b\d+\.\d{2}\b", full)]
    total = floats[-1] if floats else None

    data["totals"]["total"] = total
    data["totals"]["gst_amount"] = gst_amount

    # GST %
    gst_percent = None
    for it in data["items"]:
        if it.get("gst_percent"):
            gst_percent = it["gst_percent"]
            break

    data["totals"]["gst_percent"] = gst_percent

    # ---- SUBTOTAL ----
    if total and gst_amount:
        data["totals"]["subtotal"] = round(total - gst_amount, 2)
    else:
        data["totals"]["subtotal"] = None

    # ---- PAYMENT TERMS ----
    data["payment_terms"]["amount_due"] = total
    data["payment_terms"]["due_date"] = data["invoice_details"]["due_date"]

    return data


# ----------------------------------------------------------
# PDF → IMAGES → OCR
# ----------------------------------------------------------
def extract_invoice_text(pdf_path):
    """Convert PDF pages to images using PyMuPDF then run OCR."""
    lines = []
    doc = fitz.open(pdf_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            img_path = os.path.join(tmpdir, f"page_{i}.png")
            pix.save(img_path)
            lines.extend(reader.readtext(img_path, detail=0))

    return {
        "raw_text": lines,
        "extracted": extract_invoice_fields(lines)
    }


# ----------------------------------------------------------
# API ENDPOINT
# ----------------------------------------------------------
@app.post("/extract-invoice")
async def extract_invoice(file: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(file.file, tmp)
        pdf_path = tmp.name

    result = extract_invoice_text(pdf_path)
    os.remove(pdf_path)
    return result
