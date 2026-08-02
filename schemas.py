"""
schemas.py
==========
Pydantic request/response models for the FastAPI backend.
"""
from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class InvoiceValidateRequest(BaseModel):
    supplier_gstin: str
    buyer_gstin: Optional[str] = None
    seller_state_code: Optional[str] = None
    buyer_state_code: Optional[str] = None
    place_of_supply_code: Optional[str] = None
    taxable_amount: float = 0.0
    cgst_amount: Optional[float] = None
    sgst_amount: Optional[float] = None
    igst_amount: Optional[float] = None
    invoice_number: Optional[str] = None
    item_tax_rate: float = 18.0


class FlagOut(BaseModel):
    field: str
    severity: str
    message: str
    expected: Optional[str] = None
    found: Optional[str] = None
    rule_code: Optional[str] = None


class InvoiceValidateResponse(BaseModel):
    is_valid: bool
    gstin: str
    state_code: Optional[str] = None
    state_name: Optional[str] = None
    overall_severity: str
    is_valid_format: bool
    transaction_type: str
    expected_tax_type: Optional[str] = None
    pan: Optional[str] = None
    flags: List[FlagOut] = []


class GSTRMatchRequest(BaseModel):
    purchase_invoices: List[Dict[str, Any]]
    gstr2b_invoices: List[Dict[str, Any]]


class MatchResultOut(BaseModel):
    invoice_number: Optional[str] = None
    supplier_gstin: Optional[str] = None
    status: str
    discrepancies: List[str] = []
    tax_diff: float = 0.0
    matched_data: Dict[str, Any] = {}


class GSTRMatchResponse(BaseModel):
    total: int
    matched: int
    mismatched: int
    missing_in_gstr2b: int
    results: List[MatchResultOut]


class FlagsToDBRequest(BaseModel):
    flags: List[FlagOut]
    invoice_id: Optional[str] = None


class FlagsToDBResponse(BaseModel):
    records: List[Dict[str, Any]]


# ---------- Bulk invoice validation ----------

class BulkInvoiceItem(InvoiceValidateRequest):
    pass


class BulkResultItem(BaseModel):
    invoice_number: Optional[str] = None
    gstin: str
    is_valid: bool
    overall_severity: str
    transaction_type: str
    flag_count: int
    flags: List[FlagOut] = []


class BulkValidateRequest(BaseModel):
    invoices: List[BulkInvoiceItem]


class BulkValidateResponse(BaseModel):
    total: int
    green: int
    yellow: int
    red: int
    results: List[BulkResultItem]


# ---------- PDF audit report export ----------

class PDFExportRequest(BaseModel):
    firm_name: Optional[str] = "GST Audit Assistant"
    client_name: Optional[str] = None
    summary: Dict[str, Any]
    results: List[Dict[str, Any]]
