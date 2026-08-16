import re
from pydantic import BaseModel, validator
from typing import Optional, List, Dict, Any

GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"

class SignupRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class AuthResponse(BaseModel):
    token: str
    user_id: int
    email: str
    is_paid: bool
    paid_until: Optional[str]

class ClientCreate(BaseModel):
    name: str
    gstin: str

    @validator('gstin')
    def validate_gstin_format(cls, v):
        v = v.strip().upper()
        if not re.match(GSTIN_PATTERN, v):
            raise ValueError("Invalid GSTIN format.")
        return v

class ClientOut(BaseModel):
    id: int
    user_id: int
    name: str
    gstin: str
    created_at: str

class InvoiceValidateRequest(BaseModel):
    supplier_gstin: str
    buyer_gstin: Optional[str] = None
    seller_state_code: Optional[str] = None
    buyer_state_code: Optional[str] = None
    place_of_supply_code: Optional[str] = None
    invoice_number: Optional[str] = None
    taxable_amount: Optional[float] = 0.0
    cgst_amount: float = 0.0
    sgst_amount: float = 0.0
    igst_amount: float = 0.0
    item_tax_rate: Optional[float] = 18.0
    client_id: Optional[int] = None

class InvoiceValidateResponse(BaseModel):
    is_valid: bool
    gstin: str
    state_code: Optional[str]
    state_name: Optional[str]
    overall_severity: str
    is_valid_format: bool
    transaction_type: str
    expected_tax_type: Optional[str]
    pan: Optional[str]
    flags: List[dict]

class GSTRMatchRequest(BaseModel):
    purchase_invoices: List[dict]
    gstr2b_invoices: List[dict]
    client_id: Optional[int] = None

class GSTRMatchTextRequest(BaseModel):
    purchase_text: str
    gstr2b_text: str
    client_id: Optional[int] = None

class MatchResultOut(BaseModel):
    invoice_number: Optional[str]
    supplier_gstin: Optional[str]
    status: str
    discrepancies: List[str]
    tax_diff: Optional[float]
    matched_data: Optional[dict]

class GSTRMatchResponse(BaseModel):
    total: int
    matched: int
    mismatched: int
    missing_in_gstr2b: int
    results: List[MatchResultOut]

class FlagInput(BaseModel):
    field: str
    severity: str
    message: str
    expected: Optional[str] = None
    found: Optional[str] = None
    rule_code: Optional[str] = None

class FlagsToDBRequest(BaseModel):
    invoice_id: int
    flags: List[FlagInput]

class FlagsToDBResponse(BaseModel):
    records: List[dict]

class BulkValidateRequest(BaseModel):
    invoices: List[dict]
    client_id: Optional[int] = None

class BulkResultItem(BaseModel):
    invoice_number: Optional[str]
    gstin: str
    is_valid: bool
    overall_severity: str
    transaction_type: str
    flag_count: int
    flags: List[dict]

class BulkValidateResponse(BaseModel):
    total: int
    green: int
    yellow: int
    red: int
    results: List[BulkResultItem]

class PDFExportRequest(BaseModel):
    firm_name: str
    client_name: str
    results: List[dict]
    summary: dict


# --- VENDOR TRACKING SCHEMAS ---
class VendorOut(BaseModel):
    id: int
    gstin: str
    trade_name: Optional[str] = None
    total_invoices: int
    mismatch_count: int
    mismatch_pct: float
    risk_level: str
    last_seen_date: str

class VendorDetailOut(VendorOut):
    validations: List[dict]

class VendorTrendPoint(BaseModel):
    month: str
    total_invoices: int
    mismatch_count: int
    mismatch_pct: float

class VendorUpdateRequest(BaseModel):
    trade_name: Optional[str] = None