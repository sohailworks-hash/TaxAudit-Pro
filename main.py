"""
main.py
=======
FastAPI backend for AI Tax & Document Audit Assistant.
"""
from io import BytesIO, StringIO
import secrets
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from typing import Optional, List

import db
import auth
from gst_validator import GSTValidator, gst_flags_to_db_format, ValidationFlag, FlagSeverity
import gstr_matching
from gstr_matching import GSTRMatchingEngine, GSTR2BParser, MatchStatus, normalize_headers
from report_generator import generate_audit_pdf, generate_match_summary_pdf
from pydantic import BaseModel
from schemas import (
    SignupRequest, LoginRequest, AuthResponse,
    InvoiceValidateRequest, InvoiceValidateResponse,
    GSTRMatchRequest, GSTRMatchTextRequest, GSTRMatchResponse, MatchResultOut,
    FlagsToDBRequest, FlagsToDBResponse,
    BulkValidateRequest, BulkValidateResponse, BulkResultItem,
    PDFExportRequest, ClientCreate, ClientOut,
    VendorOut, VendorDetailOut, VendorTrendPoint, VendorUpdateRequest,
)

app = FastAPI(title="AI Tax & Document Audit Assistant API", version="1.0.0")

import os
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://tax-audit-pro-iota.vercel.app,http://localhost:3000,http://127.0.0.1:5500"
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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

# --- SECURITY HELPER ---
def verify_client_ownership(client_id: Optional[int], user_id: int):
    if client_id is not None:
        if not db.get_client_by_id(client_id, user_id):
            raise HTTPException(status_code=404, detail="Client not found or access denied.")

# ---------------------------------------------------------------------------
# Authentication & Access Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/auth/signup", response_model=AuthResponse)
def signup(payload: SignupRequest):
    hashed_pw = auth.get_password_hash(payload.password)
    user = db.create_user(payload.email, hashed_pw)
    if not user:
        raise HTTPException(status_code=400, detail="Email already registered.")

    token = auth.create_access_token(data={"sub": str(user["id"])})
    return AuthResponse(
        token=token,
        user_id=user["id"],
        email=user["email"],
        is_paid=False,
        paid_until=None
    )

@app.post("/api/v1/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest):
    user = db.get_user_by_email(payload.email)
    if not user or not auth.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = auth.create_access_token(data={"sub": str(user["id"])})
    return AuthResponse(
        token=token,
        user_id=user["id"],
        email=user["email"],
        is_paid=bool(user["is_paid"]),
        paid_until=user["paid_until"]
    )

@app.get("/api/v1/user/status")
def user_status(current_user: dict = Depends(auth.get_current_user)):
    return {
        "user_id": current_user["id"],
        "email": current_user["email"],
        "is_paid": bool(current_user["is_paid"]),
        "paid_until": current_user.get("paid_until"),
        "trial_used": current_user.get("trial_used", 0),
        "trial_limit": auth.FREE_TRIAL_LIMIT
    }

class RedeemRequest(BaseModel):
    code: str

class GenerateCodeRequest(BaseModel):
    admin_key: str
    duration_days: int = 30

@app.post("/api/v1/access/redeem")
def redeem_code(payload: RedeemRequest, current_user: dict = Depends(auth.get_current_user)):
    if not db.redeem_access_code(current_user["id"], payload.code):
        raise HTTPException(status_code=400, detail="Invalid or already-used access code.")
    return {"message": "Access unlocked successfully."}

@app.post("/api/v1/admin/generate-code")
def admin_generate_code(payload: GenerateCodeRequest):
    if not secrets.compare_digest(payload.admin_key, auth.ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Invalid admin key.")

    code = auth.generate_code()
    db.generate_access_code(code, payload.duration_days)
    return {"code": code, "duration_days": payload.duration_days}


# ---------------------------------------------------------------------------
# Client Management Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/clients", response_model=ClientOut)
def add_client(payload: ClientCreate, current_user: dict = Depends(auth.get_current_user)):
    try:
        client = db.create_client(current_user["id"], payload.name, payload.gstin)
        return client
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to add client: {e}")

@app.get("/api/v1/clients", response_model=List[ClientOut])
def list_clients(current_user: dict = Depends(auth.get_current_user)):
    return db.get_clients_by_user(current_user["id"])

@app.get("/api/v1/clients/{client_id}", response_model=ClientOut)
def get_client(client_id: int, current_user: dict = Depends(auth.get_current_user)):
    client = db.get_client_by_id(client_id, current_user["id"])
    if not client:
        raise HTTPException(status_code=404, detail="Client not found or unauthorized access.")
    return client

@app.delete("/api/v1/clients/{client_id}")
def delete_client(client_id: int, current_user: dict = Depends(auth.get_current_user)):
    success = db.delete_client(client_id, current_user["id"])
    if not success:
        raise HTTPException(status_code=404, detail="Client not found or unauthorized access.")
    return {"message": "Client deleted successfully."}


# ---------------------------------------------------------------------------
# Vendor Tracking Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v1/vendors", response_model=List[VendorOut])
def list_vendors(current_user: dict = Depends(auth.get_current_user)):
    return db.get_vendors_by_user(current_user["id"])

@app.get("/api/v1/vendors/{vendor_id}", response_model=VendorDetailOut)
def get_vendor(vendor_id: int, current_user: dict = Depends(auth.get_current_user)):
    vendor = db.get_vendor_detail(vendor_id, current_user["id"])
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found or access denied.")
    return vendor

@app.get("/api/v1/vendors/{vendor_id}/trend", response_model=List[VendorTrendPoint])
def get_vendor_trend(vendor_id: int, current_user: dict = Depends(auth.get_current_user)):
    trend = db.get_vendor_trend(vendor_id, current_user["id"])
    if trend is None:
        raise HTTPException(status_code=404, detail="Vendor not found or access denied.")
    return trend

@app.patch("/api/v1/vendors/{vendor_id}")
def update_vendor(vendor_id: int, payload: VendorUpdateRequest, current_user: dict = Depends(auth.get_current_user)):
    if payload.trade_name is None:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    success = db.update_vendor_trade_name(vendor_id, current_user["id"], payload.trade_name)
    if not success:
        raise HTTPException(status_code=404, detail="Vendor not found or access denied.")
    return {"message": "Vendor updated successfully."}


# ---------------------------------------------------------------------------
# Core Validation & Matching Operations
# ---------------------------------------------------------------------------

@app.post("/api/v1/validate-invoice", response_model=InvoiceValidateResponse)
def validate_invoice(payload: InvoiceValidateRequest, current_user: dict = Depends(auth.get_current_user)):
    auth.enforce_trial_limit(current_user)
    verify_client_ownership(payload.client_id, current_user["id"])

    val_payload = payload.model_dump()
    client_id = val_payload.pop("client_id", None)

    try:
        result = GSTValidator.validate_invoice(**val_payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {e}")

    db.log_validation(
        user_id=current_user["id"],
        client_id=client_id,
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


def _build_match_response(purchase_invoices, gstr2b_invoices, source: str, user_id: int, client_id: int = None, column_warnings: list = None) -> GSTRMatchResponse:
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

    db.link_vendors_from_matches(user_id, results, client_id)
    db.log_match_summary(total=len(out), matched=matched, mismatched=mismatched, missing=missing, source=source, user_id=user_id, client_id=client_id)

    return GSTRMatchResponse(
        total=len(out),
        matched=matched,
        mismatched=mismatched,
        missing_in_gstr2b=missing,
        results=out,
        column_warnings=column_warnings or [],
    )


@app.post("/api/v1/match-gstr", response_model=GSTRMatchResponse)
def match_gstr(payload: GSTRMatchRequest, current_user: dict = Depends(auth.get_current_user)):
    auth.enforce_trial_limit(current_user)
    verify_client_ownership(payload.client_id, current_user["id"])
    return _build_match_response(payload.purchase_invoices, payload.gstr2b_invoices, source="json", user_id=current_user["id"], client_id=payload.client_id)


@app.post("/api/v1/match-gstr-text", response_model=GSTRMatchResponse)
def match_gstr_text(payload: GSTRMatchTextRequest, current_user: dict = Depends(auth.get_current_user)):
    auth.enforce_trial_limit(current_user)
    verify_client_ownership(payload.client_id, current_user["id"])
    purchase_records = gstr_matching.parse_raw_text(payload.purchase_text)
    gstr2b_records = gstr_matching.parse_raw_text(payload.gstr2b_text)
    if not purchase_records or not gstr2b_records:
        raise HTTPException(status_code=400, detail="Could not detect any GSTIN/invoice records in the pasted text.")
    return _build_match_response(purchase_records, gstr2b_records, source="text", user_id=current_user["id"], client_id=payload.client_id)


@app.post("/api/v1/match-gstr-file", response_model=GSTRMatchResponse)
async def match_gstr_file(
    purchase_file: UploadFile = File(...),
    gstr2b_file: UploadFile = File(...),
    client_id: Optional[int] = Form(None),
    current_user: dict = Depends(auth.get_current_user),
):
    auth.enforce_trial_limit(current_user)
    verify_client_ownership(client_id, current_user["id"])

    warnings = []

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
                text = raw.decode("utf-8-sig", errors="ignore")
                records = GSTR2BParser.parse_csv(text)
                try:
                    missing = gstr_matching.missing_critical_fields(records[0]) if records else []
                    if missing:
                        warnings.append(f"{file.filename}: couldn't find a column for {', '.join(missing)} — please check this data made it through correctly.")
                except Exception:
                    pass
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

    return _build_match_response(purchase_invoices, gstr2b_invoices, source="file", user_id=current_user["id"], client_id=client_id, column_warnings=warnings)


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


# --- BULK PROCESSING ---

def _validate_single(item_dict: dict, user_id: int, client_id: int = None) -> BulkResultItem:
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
        user_id=user_id,
        client_id=client_id,
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
def validate_bulk_invoices(payload: BulkValidateRequest, current_user: dict = Depends(auth.get_current_user)):
    auth.enforce_trial_limit(current_user)
    verify_client_ownership(payload.client_id, current_user["id"])
    if not payload.invoices:
        raise HTTPException(status_code=400, detail="No invoices provided.")

    results = [_validate_single(inv, current_user["id"], payload.client_id) for inv in payload.invoices]
    return _summarize_bulk(results)


def _parse_invoice_file(raw: bytes, filename: str) -> list:
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
async def validate_bulk_invoices_file(
    file: UploadFile = File(...),
    client_id: Optional[int] = Form(None),
    current_user: dict = Depends(auth.get_current_user)
):
    auth.enforce_trial_limit(current_user)
    verify_client_ownership(client_id, current_user["id"])
    try:
        raw = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")

    records = _parse_invoice_file(raw, file.filename)
    if not records:
        raise HTTPException(status_code=400, detail="No valid invoice rows found in file.")

    results = [_validate_single(r, current_user["id"], client_id) for r in records]
    return _summarize_bulk(results)


# ---------------------------------------------------------------------------
# PDF Exports & Audit History
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


@app.post("/api/v1/export-match-pdf")
def export_match_pdf(payload: PDFExportRequest):
    try:
        pdf_bytes = generate_match_summary_pdf(payload.firm_name, payload.client_name, payload.results, payload.summary)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=gstr2b_match_report.pdf"},
    )


@app.get("/api/v1/audit-history/validations")
def audit_history_validations(
    limit: int = 20, offset: int = 0, severity: str = None,
    date_from: str = None, date_to: str = None, search: str = None,
    current_user: dict = Depends(auth.get_current_user)
):
    try:
        total, rows = db.query_validations(user_id=current_user["id"], limit=limit, offset=offset, severity=severity, date_from=date_from, date_to=date_to, search=search)
        return {"total": total, "limit": limit, "offset": offset, "results": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read history: {e}")


@app.get("/api/v1/audit-history/matches")
def audit_history_matches(
    limit: int = 20, offset: int = 0, date_from: str = None, date_to: str = None,
    current_user: dict = Depends(auth.get_current_user)
):
    try:
        total, rows = db.query_matches(user_id=current_user["id"], limit=limit, offset=offset, date_from=date_from, date_to=date_to)
        return {"total": total, "limit": limit, "offset": offset, "results": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read history: {e}")


def _rows_to_csv(rows: list, fieldnames: list) -> str:
    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


@app.get("/api/v1/audit-history/validations/export-csv")
def export_validations_csv(
    severity: str = None, date_from: str = None, date_to: str = None, search: str = None,
    current_user: dict = Depends(auth.get_current_user)
):
    try:
        _, rows = db.query_validations(user_id=current_user["id"], limit=100000, offset=0, severity=severity,
                                        date_from=date_from, date_to=date_to, search=search)
        csv_text = _rows_to_csv(rows, ["id", "user_id", "device_id", "client_id", "vendor_id", "gstin", "invoice_number", "overall_severity",
                                        "is_valid", "transaction_type", "flag_count", "created_at"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    return Response(content=csv_text, media_type="text/csv",
                     headers={"Content-Disposition": "attachment; filename=validation_history.csv"})


@app.get("/api/v1/audit-history/matches/export-csv")
def export_matches_csv(
    date_from: str = None, date_to: str = None,
    current_user: dict = Depends(auth.get_current_user)
):
    try:
        _, rows = db.query_matches(user_id=current_user["id"], limit=100000, offset=0, date_from=date_from, date_to=date_to)
        csv_text = _rows_to_csv(rows, ["id", "user_id", "device_id", "client_id", "total", "matched", "mismatched",
                                        "missing_in_gstr2b", "source", "created_at"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    return Response(content=csv_text, media_type="text/csv",
                     headers={"Content-Disposition": "attachment; filename=match_history.csv"})