"""
main.py
=======
FastAPI backend for AI Tax & Document Audit Assistant.
"""
from io import BytesIO, StringIO

import secrets
import re
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

import db
import auth
from gst_validator import GSTValidator, gst_flags_to_db_format, ValidationFlag, FlagSeverity
from gstr_matching import GSTRMatchingEngine, GSTR2BParser, MatchStatus, normalize_headers
from report_generator import generate_audit_pdf
from pydantic import BaseModel
from schemas import (
    InvoiceValidateRequest, InvoiceValidateResponse,
    GSTRMatchRequest, GSTRMatchResponse, MatchResultOut,
    FlagsToDBRequest, FlagsToDBResponse,
    BulkValidateRequest, BulkValidateResponse, BulkResultItem,
    PDFExportRequest,
)

app = FastAPI(title="AI Tax & Document Audit Assistant API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    db.init_db()


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Device-based trial + access code system
# ---------------------------------------------------------------------------

class RedeemRequest(BaseModel):
    device_id: str
    code: str

class GenerateCodeRequest(BaseModel):
    admin_key: str


@app.get("/api/v1/device/status")
def device_status(device: dict = Depends(auth.get_device)):
    return {"device_id": device["device_id"], "is_paid": bool(device["is_paid"]),
            "trial_used": device["trial_used"], "trial_limit": auth.FREE_TRIAL_LIMIT}


@app.post("/api/v1/access/redeem")
def redeem_code(payload: RedeemRequest):
    if not db.redeem_access_code(payload.device_id, payload.code):
        raise HTTPException(status_code=400, detail="Invalid or already-used access code.")
    return {"message": "Access unlocked successfully."}


@app.post("/api/v1/admin/generate-code")
def admin_generate_code(payload: GenerateCodeRequest):
    if payload.admin_key != auth.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")
    code = auth.generate_code()
    db.generate_access_code(code)
    return {"code": code}


# ---------------------------------------------------------------------------
# Single invoice validation
# ---------------------------------------------------------------------------

@app.post("/api/v1/validate-invoice", response_model=InvoiceValidateResponse)
def validate_invoice(payload: InvoiceValidateRequest, device: dict = Depends(auth.get_device)):
    auth.enforce_trial_limit(device)
    try:
        result = GSTValidator.validate_invoice(**payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {e}")

    db.log_validation(
        gstin=result.gstin,
        invoice_number=payload.invoice_number,
        severity=result.overall_severity,
        is_valid=result.is_valid,
        tx_type=result.transaction_type.value,
        flag_count=len(result.flags),
    )

    return InvoiceValidateResponse(
        is_valid=result.is_valid,
        gstin=result.gstin,
        state_code=result.state_code,
        state_name=result.state_name,
        overall_severity=result.overall_severity,
        is_valid_format=result.is_valid_format,
        transaction_type=result.transaction_type.value,
        expected_tax_type=result.expected_tax_type,
        pan=result.pan,
        flags=[
            {
                "field": f.field,
                "severity": f.severity.value if isinstance(f.severity, FlagSeverity) else f.severity,
                "message": f.message,
                "expected": f.expected,
                "found": f.found,
                "rule_code": f.rule_code,
            }
            for f in result.flags
        ],
    )


# ---------------------------------------------------------------------------
# GSTR-2B matching
# ---------------------------------------------------------------------------

def _build_match_response(purchase_invoices, gstr2b_invoices, source: str) -> GSTRMatchResponse:
    if not purchase_invoices:
        raise HTTPException(status_code=400, detail="No valid purchase records found.")

    try:
        results = GSTRMatchingEngine.run_matching(purchase_invoices, gstr2b_invoices)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Matching error: {e}")

    out = [
        MatchResultOut(
            invoice_number=r.invoice_number,
            supplier_gstin=r.supplier_gstin,
            status=r.status.value,
            discrepancies=r.discrepancies,
            tax_diff=r.tax_diff,
            matched_data=r.matched_data,
        )
        for r in results
    ]

    matched = sum(1 for r in results if r.status == MatchStatus.MATCHED)
    mismatched = sum(1 for r in results if r.status == MatchStatus.MISMATCHED)
    missing = sum(1 for r in results if r.status == MatchStatus.MISSING_IN_GSTR2B)

    db.log_match_summary(len(out), matched, mismatched, missing, source)

    return GSTRMatchResponse(
        total=len(out),
        matched=matched,
        mismatched=mismatched,
        missing_in_gstr2b=missing,
        results=out,
    )


@app.post("/api/v1/match-gstr", response_model=GSTRMatchResponse)
def match_gstr(payload: GSTRMatchRequest, device: dict = Depends(auth.get_device)):
    """Reconciliation via raw JSON payload."""
    auth.enforce_trial_limit(device)
    return _build_match_response(payload.purchase_invoices, payload.gstr2b_invoices, source="json")


@app.post("/api/v1/match-gstr-file", response_model=GSTRMatchResponse)
async def match_gstr_file(
    purchase_file: UploadFile = File(...),
    gstr2b_file: UploadFile = File(...),
    device: dict = Depends(auth.get_device),
):
    """Reconciliation via uploaded CSV or Excel (.xlsx/.xls) files."""
    auth.enforce_trial_limit(device)

    async def parse(file: UploadFile):
        name = (file.filename or "").lower()
        try:
            raw = await file.read()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not read '{file.filename}': {e}")

        try:
            if name.endswith((".xlsx", ".xls")):
                records = GSTR2BParser.parse_excel(raw)
            elif name.endswith(".csv"):
                records = GSTR2BParser.parse_csv(raw.decode("utf-8-sig", errors="ignore"))
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported file type: '{file.filename}'. Use .csv, .xlsx, or .xls.")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse '{file.filename}': {e}")

        if not records:
            raise HTTPException(
                status_code=400,
                detail=f"No valid rows found in '{file.filename}'. Ensure it has invoice_number and supplier_gstin columns.",
            )
        return records

    purchase_invoices = await parse(purchase_file)
    gstr2b_invoices = await parse(gstr2b_file)

    return _build_match_response(purchase_invoices, gstr2b_invoices, source="file")


# ---------------------------------------------------------------------------
# Flags to DB format
# ---------------------------------------------------------------------------

@app.post("/api/v1/flags-to-db", response_model=FlagsToDBResponse)
def flags_to_db(payload: FlagsToDBRequest):
    try:
        flag_objs = [
            ValidationFlag(
                field=f.field,
                severity=FlagSeverity(f.severity),
                message=f.message,
                expected=f.expected,
                found=f.found,
                rule_code=f.rule_code,
            )
            for f in payload.flags
        ]
        records = gst_flags_to_db_format(flag_objs, invoice_id=payload.invoice_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Conversion error: {e}")

    return FlagsToDBResponse(records=records)


# ---------------------------------------------------------------------------
# Bulk invoice validation
# ---------------------------------------------------------------------------

def _validate_single(item_dict: dict) -> BulkResultItem:
    """Validates one invoice dict, logs it, and returns a summarized result.
    Never raises — processing errors for a single row are captured as a RED flag
    so one bad row doesn't fail the whole bulk batch."""
    invoice_number = item_dict.get("invoice_number")
    gstin = item_dict.get("supplier_gstin", "") or ""

    allowed_keys = {
        "supplier_gstin", "buyer_gstin", "seller_state_code", "buyer_state_code",
        "place_of_supply_code", "taxable_amount", "cgst_amount", "sgst_amount",
        "igst_amount", "invoice_number", "item_tax_rate",
    }
    clean_kwargs = {k: v for k, v in item_dict.items() if k in allowed_keys}

    try:
        result = GSTValidator.validate_invoice(**clean_kwargs)
    except Exception as e:
        return BulkResultItem(
            invoice_number=invoice_number,
            gstin=gstin,
            is_valid=False,
            overall_severity="RED",
            transaction_type="UNKNOWN",
            flag_count=1,
            flags=[{
                "field": "system",
                "severity": "RED",
                "message": f"Processing error: {e}",
                "expected": None,
                "found": None,
                "rule_code": "SYS_001",
            }],
        )

    db.log_validation(
        gstin=result.gstin,
        invoice_number=invoice_number,
        severity=result.overall_severity,
        is_valid=result.is_valid,
        tx_type=result.transaction_type.value,
        flag_count=len(result.flags),
    )

    return BulkResultItem(
        invoice_number=invoice_number,
        gstin=result.gstin,
        is_valid=result.is_valid,
        overall_severity=result.overall_severity,
        transaction_type=result.transaction_type.value,
        flag_count=len(result.flags),
        flags=[
            {
                "field": f.field,
                "severity": f.severity.value if isinstance(f.severity, FlagSeverity) else f.severity,
                "message": f.message,
                "expected": f.expected,
                "found": f.found,
                "rule_code": f.rule_code,
            }
            for f in result.flags
        ],
    )


def _summarize_bulk(results: list) -> BulkValidateResponse:
    return BulkValidateResponse(
        total=len(results),
        green=sum(1 for r in results if r.overall_severity == "GREEN"),
        yellow=sum(1 for r in results if r.overall_severity == "YELLOW"),
        red=sum(1 for r in results if r.overall_severity == "RED"),
        results=results,
    )


@app.post("/api/v1/validate-bulk-invoices", response_model=BulkValidateResponse)
def validate_bulk_invoices(payload: BulkValidateRequest, device: dict = Depends(auth.get_device)):
    """Validates a JSON array of invoices in one call."""
    auth.enforce_trial_limit(device)
    if not payload.invoices:
        raise HTTPException(status_code=400, detail="No invoices provided.")

    results = [_validate_single(inv.model_dump()) for inv in payload.invoices]
    return _summarize_bulk(results)


def _parse_invoice_file(raw: bytes, filename: str) -> list:
    """Parses a CSV or Excel file of invoices into a list of dicts."""
    try:
        import pandas as pd
    except ImportError:
        raise HTTPException(status_code=500, detail="pandas is not installed on the server.")

    name = (filename or "").lower()
    try:
        if name.endswith(".xlsx"):
            df = pd.read_excel(BytesIO(raw), engine="openpyxl")
        elif name.endswith(".xls"):
            df = pd.read_excel(BytesIO(raw))
        elif name.endswith(".csv"):
            df = pd.read_csv(StringIO(raw.decode("utf-8-sig", errors="ignore")))
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: '{filename}'. Use .csv, .xlsx, or .xls.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse '{filename}': {e}")

    if df is None or df.empty:
        return []

    df.columns = [normalize_headers(df.columns)[c] for c in df.columns]
    if "supplier_gstin" not in df.columns:
        raise HTTPException(status_code=400, detail="File must contain a 'supplier_gstin' column.")

    numeric_cols = ["taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "item_tax_rate"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["supplier_gstin"])
    records = df.where(pd.notnull(df), None).to_dict(orient="records")

    cleaned = []
    for r in records:
        item = {k: v for k, v in r.items() if v is not None}
        if "supplier_gstin" in item:
            item["supplier_gstin"] = str(item["supplier_gstin"]).strip()
        if "invoice_number" in item:
            item["invoice_number"] = str(item["invoice_number"]).strip()
        for state_field in ("place_of_supply_code", "seller_state_code", "buyer_state_code"):
            if state_field in item:
                raw_val = str(item[state_field]).strip()
                if raw_val.endswith(".0"):
                    raw_val = raw_val[:-2]
                item[state_field] = raw_val.zfill(2) if raw_val.isdigit() else raw_val
        cleaned.append(item)
    return cleaned


@app.post("/api/v1/validate-bulk-invoices-file", response_model=BulkValidateResponse)
async def validate_bulk_invoices_file(file: UploadFile = File(...), device: dict = Depends(auth.get_device)):
    """Validates a batch of invoices uploaded as a CSV or Excel file."""
    auth.enforce_trial_limit(device)
    try:
        raw = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")

    records = _parse_invoice_file(raw, file.filename)
    if not records:
        raise HTTPException(status_code=400, detail="No valid invoice rows found in file.")

    results = [_validate_single(r) for r in records]
    return _summarize_bulk(results)


# ---------------------------------------------------------------------------
# PDF audit report export
# ---------------------------------------------------------------------------

@app.post("/api/v1/export-audit-pdf")
def export_audit_pdf(payload: PDFExportRequest):
    try:
        pdf_bytes = generate_audit_pdf(payload.firm_name, payload.client_name, payload.results, payload.summary)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=gst_audit_report.pdf"},
    )


# ---------------------------------------------------------------------------
# Audit history
# ---------------------------------------------------------------------------

@app.get("/api/v1/audit-history/validations")
def audit_history_validations(
    limit: int = 20, offset: int = 0, severity: str = None,
    date_from: str = None, date_to: str = None, search: str = None,
):
    try:
        total, rows = db.query_validations(limit, offset, severity, date_from, date_to, search)
        return {"total": total, "limit": limit, "offset": offset, "results": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read history: {e}")


@app.get("/api/v1/audit-history/matches")
def audit_history_matches(limit: int = 20, offset: int = 0, date_from: str = None, date_to: str = None):
    try:
        total, rows = db.query_matches(limit, offset, date_from, date_to)
        return {"total": total, "limit": limit, "offset": offset, "results": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read history: {e}")
