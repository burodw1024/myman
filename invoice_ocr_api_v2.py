import os
import re
import tempfile
import shutil
from fastapi import FastAPI, UploadFile, File
from pdf2image import convert_from_path
import easyocr
import dateparser

app = FastAPI(title="Stable Invoice OCR - Final Version (GST Both)")

reader = easyocr.Reader(["en"], gpu=False)


# ----------------------------------------------------------
# Date Finder
# ----------------------------------------------------------
def find_date_in_window(lines, start_idx, window=6):
    for i in range(start_idx + 1, min(start_idx + window, len(lines))):
        dt = dateparser.parse(lines[i])
        if dt:
            return dt.strftime("%d %b %Y")
    return None


# ----------------------------------------------------------
# Australian Supplier Address Extractor
# ----------------------------------------------------------
def extract_supplier_address(lines):
    address = []
    capturing = False

    START = ["level", "suite", "elizabeth"]
    STREET_WORDS = ["st", "street", "road", "rd", "ave", "avenue"]
    CITY_WORDS = ["melbourne", "sydney", "brisbane", "perth", "adelaide", "hobart"]
    STOP = ["customer", "payment", "invoice", "amount", "description", "quantity"]

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
            any(s in low for s in START)
            or any(s in low for s in STREET_WORDS)
            or any(s in low for s in CITY_WORDS)
            or "australia" in low
        ):
            address.append(line)

        if "australia" in low:
            break

    return ", ".join(address).replace("  ", " ").strip()


# ----------------------------------------------------------
# ABN Extraction
# ----------------------------------------------------------
def extract_abn(text):
    m = re.search(r"ABN[\s:]*([\d ]{11,20})", text, re.IGNORECASE)
    if m:
        return m.group(1).replace(" ", "")
    return None


# ----------------------------------------------------------
# Multi-line Table Item Extractor (Safe)
# ----------------------------------------------------------
def extract_items(lines):
    cleaned = [x.strip() for x in lines if x.strip()]

    items = []
    bucket = []

    numeric = re.compile(r"^\d+(\.\d{1,2})?$")
    money = re.compile(r"^\d+\.\d{2}$")
    gst_percent = re.compile(r"^\d+%$")

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

        nums = [x for x in group if numeric.match(x) or money.match(x) or gst_percent.match(x)]

        if len(nums) >= 4:
            qty = nums[0]
            unit = nums[1]
            gstp = nums[2] if gst_percent.match(nums[2]) else None
            total = nums[3]

            desc = " ".join([
                x for x in group
                if x not in nums
                and not re.match(r"^(item|description|quantity|gst|amount)", x.lower())
                and "amount aud" not in x.lower()
                and "tixperts-" not in x.lower()
            ]).strip()

            items.append({
                "description": desc,
                "quantity": qty,
                "unit_price": unit,
                "gst_percent": gstp,
                "line_total": total
            })

            group = []

    return items


# ----------------------------------------------------------
# Main Extraction Logic
# ----------------------------------------------------------
def extract_invoice_fields(lines):
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

    # Invoice Number
    inv = re.findall(r"[A-Z]{2}\.\d{3}-\d{2}\.INV-\d{4}", full)
    data["invoice_details"]["invoice_number"] = inv[0] if inv else None

    # Invoice Date
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

    # Due Date
    due = re.search(r"Due Date[:\s]+(\d{1,2} \w+ \d{4})", full)
    data["invoice_details"]["due_date"] = due.group(1) if due else None

    # Supplier
    supplier = None
    for t in safe:
        if "pty" in t.lower():
            supplier = t.replace("TIA", "T/A")
            break

    data["supplier"]["name"] = supplier
    data["supplier"]["address"] = extract_supplier_address(safe)
    data["supplier"]["abn"] = extract_abn(full)

    # Customer Name
    for idx, line in enumerate(safe):
        if "customer" in line.lower() and idx + 1 < len(safe):
            data["customer"]["name"] = safe[idx + 1]
            break

    # Items
    items = extract_items(safe)
    data["items"] = items

    # GST amount (decimal even if on next line)
    gst_amount = None

    # first try same-line: "INCLUDES GST 7.73"
    m = re.search(r"INCLUDES GST[^\d]*([\d]+\.[\d]+)", full, re.IGNORECASE)
    if m:
        gst_amount = float(m.group(1))
    else:
        # look ahead lines after "INCLUDES GST"
        for idx, line in enumerate(safe):
            if "includes gst" in line.lower():
                for nxt in safe[idx:idx+5]:
                    mm = re.search(r"([\d]+\.[\d]+)", nxt)
                    if mm:
                        gst_amount = float(mm.group(1))
                        break
                break

    # Total
    floats = [float(x) for x in re.findall(r"\b\d+\.\d{2}\b", full)]
    total = floats[-1] if floats else None

    data["totals"]["total"] = total
    data["totals"]["gst_amount"] = gst_amount

    # GST Percent
    gst_percent_val = None
    for item in items:
        if item.get("gst_percent"):
            gst_percent_val = item["gst_percent"]
            break

    data["totals"]["gst_percent"] = gst_percent_val

    # Subtotal
    if total and gst_amount:
        data["totals"]["subtotal"] = round(total - gst_amount, 2)
    else:
        data["totals"]["subtotal"] = None

    # Payment Terms
    data["payment_terms"]["amount_due"] = total
    data["payment_terms"]["due_date"] = data["invoice_details"]["due_date"]

    return data


# ----------------------------------------------------------
# PDF → OCR → JSON
# ----------------------------------------------------------
def extract_invoice_text(pdf_path):
    pages = convert_from_path(pdf_path, dpi=300)
    lines = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, page in enumerate(pages):
            img_path = os.path.join(tmpdir, f"page_{i}.png")
            page.save(img_path, "PNG")
            lines.extend(reader.readtext(img_path, detail=0))

    return {
        "raw_text": lines,
        "extracted": extract_invoice_fields(lines)
    }


# ----------------------------------------------------------
# FastAPI Endpoint
# ----------------------------------------------------------
@app.post("/extract-invoice")
async def extract_invoice(file: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(file.file, tmp)
        pdf_path = tmp.name

    result = extract_invoice_text(pdf_path)
    os.remove(pdf_path)
    return result
