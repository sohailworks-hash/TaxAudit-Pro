"""
gstr_matching.py
================
GSTR-2B Matching Engine for AI Tax & Document Audit Assistant
"""
import csv
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any, List, Dict


# ---------------------------------------------------------------------------
# Column header normalization — lets users upload CSV/Excel with any
# reasonable column naming (including real GSTR-2B portal exports and
# Tally purchase register exports) and still have it map to our canonical
# fields.
# ---------------------------------------------------------------------------
COLUMN_ALIASES = {
    "invoice_number": [
        "invoiceno", "invoicenum", "invno", "invnumber", "billno", "billnumber",
        "invoice", "invoiceid", "docno", "documentnumber", "invoicenumber",
        "supplierinvoiceno", "supplierinvoicenumber",
    ],
    "supplier_gstin": [
        "gstin", "suppliergst", "vendorgstin", "sellergstin", "gstno", "gstnumber",
        "gst", "supplierid", "gstinofsupplier", "gstinuin",
    ],
    "buyer_gstin": ["buyergst", "customergstin", "recipientgstin"],
    "tax_amount": ["taxamt", "taxvalue", "gstamount", "totaltax", "taxtotal", "amount", "invoicetax"],
    "taxable_amount": ["taxable", "taxablevalue", "basicamount", "baseamount", "netamount", "assessablevalue"],
    "cgst_amount": ["cgst", "cgstamt", "cgstvalue", "centraltax"],
    "sgst_amount": ["sgst", "sgstamt", "sgstvalue", "stateuttax", "sgstutgst"],
    "igst_amount": ["igst", "igstamt", "igstvalue", "integratedtax"],
    "cess_amount": ["cess", "cessamt", "cessvalue"],
    "place_of_supply_code": ["placeofsupply", "pos", "supplyplace", "poscode"],
    "seller_state_code": ["sellerstate", "supplierstate", "statecode"],
    "buyer_state_code": ["buyerstate", "customerstate"],
    "item_tax_rate": ["taxrate", "rate", "gstrate", "itemtaxrate", "applicableoftaxrate", "taxrateapplicable"],
    "supplier_name": ["tradelegalname", "supplier", "particulars", "suppliername", "vendorname", "partyname"],
    "invoice_date": ["invoicedate", "supplierinvoicedate", "billdate"],
    "invoice_type": ["invoicetype"],
    "invoice_value": ["invoicevalue"],
    "reverse_charge": ["supplyattractreversecharge", "reversecharge", "rcm"],
    "return_period": ["gstr11aiffgstr5period", "returnperiod", "period"],
    "filing_date": ["gstr11aiffgstr5filingdate", "filingdate"],
    "itc_availability": ["itcavailability"],
    "itc_reason": ["reason"],
    "source": ["source"],
    "voucher_type": ["vouchertype"],
    "voucher_number": ["voucherno", "vouchernumber"],
    "voucher_date": ["date"],
    "gross_total": ["grosstotal"],
    "addl_cost": ["addlcost", "additionalcost"],
}
_ALIAS_LOOKUP = {}
for canonical, aliases in COLUMN_ALIASES.items():
    _ALIAS_LOOKUP[canonical] = canonical
    for a in aliases:
        _ALIAS_LOOKUP[a] = canonical


def _normalize_key(k: str) -> str:
    """Lowercase and strip ALL non-alphanumeric chars (spaces, ₹, /, (), ., #, -)."""
    return re.sub(r"[^a-z0-9]", "", str(k).strip().lower())


def normalize_headers(keys) -> Dict[str, str]:
    """Maps raw column headers to canonical field names where recognized.
    Unrecognized columns are kept as-is (normalized to snake_case)."""
    mapping = {}
    for k in keys:
        norm = _normalize_key(k)
        canonical = _ALIAS_LOOKUP.get(norm)
        mapping[k] = canonical if canonical else str(k).strip().lower().replace(" ", "_")
    return mapping


def remap_row(row: dict) -> dict:
    mapping = normalize_headers(row.keys())
    return {mapping[k]: v for k, v in row.items()}


def _derive_amounts(df):
    """Fills tax_amount / taxable_amount from split IGST/CGST/SGST/Cess
    columns when the source file (e.g. real GSTR-2B or Tally exports)
    doesn't give a single combined column."""
    import pandas as pd

    tax_parts = [c for c in ["igst_amount", "cgst_amount", "sgst_amount", "cess_amount"] if c in df.columns]
    for c in tax_parts:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    if "tax_amount" not in df.columns and tax_parts:
        df["tax_amount"] = df[tax_parts].sum(axis=1)
    elif "tax_amount" in df.columns:
        df["tax_amount"] = pd.to_numeric(df["tax_amount"], errors="coerce").fillna(0.0)

    # Tally exports sometimes only give "Gross Total" (taxable + tax) with
    # no clean taxable-value column -> derive it.
    if "taxable_amount" not in df.columns and "gross_total" in df.columns and tax_parts:
        gross = pd.to_numeric(df["gross_total"], errors="coerce").fillna(0.0)
        df["taxable_amount"] = gross - df[tax_parts].sum(axis=1)
    elif "taxable_amount" in df.columns:
        df["taxable_amount"] = pd.to_numeric(df["taxable_amount"], errors="coerce")

    return df


def _invoice_number_key(inv: Optional[str]) -> str:
    """Normalizes an invoice number for matching so formats like
    '26-27/0032', '0032', and '32' are all treated as the same invoice
    (portal exports often prefix invoice numbers with the financial year,
    while accounting software like Tally often stores just the serial)."""
    if inv is None:
        return ""
    digits_groups = re.findall(r"\d+", str(inv))
    if digits_groups:
        return str(int(digits_groups[-1]))  # last numeric run, leading zeros stripped
    return str(inv).strip().lower()


class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    MISSING_IN_GSTR2B = "MISSING_IN_GSTR2B"
    PORTAL_ONLY = "PORTAL_ONLY"


@dataclass
class MatchResult:
    invoice_number: str
    supplier_gstin: str
    status: MatchStatus
    discrepancies: List[str] = field(default_factory=list)
    tax_diff: float = 0.0
    matched_data: Dict[str, Any] = field(default_factory=dict)


class GSTR2BParser:
    @staticmethod
    def parse_json(file_content: str) -> List[Dict[str, Any]]:
        """Parses GSTR-2B data from JSON format."""
        try:
            data = json.loads(file_content)
            if isinstance(data, list):
                return [item for item in data if item.get("supplier_gstin")]
            elif isinstance(data, dict) and "invoices" in data:
                return [item for item in data["invoices"] if item.get("supplier_gstin")]
            return []
        except Exception:
            return []

    @staticmethod
    def parse_csv(file_content: str) -> List[Dict[str, Any]]:
        """Parses GSTR-2B data from CSV format. Accepts flexible column names."""
        invoices = []
        try:
            reader = csv.DictReader(file_content.splitlines())
            for row in reader:
                row = remap_row(row)
                if row.get("supplier_gstin"):
                    invoices.append(row)
        except Exception:
            pass
        return invoices

    @staticmethod
    def _find_header_row(raw_df) -> Optional[int]:
        """Scans the first ~20 rows of a headerless sheet to find the real
        header row (handles junk/title rows above the header, common in
        official GSTR-2B exports)."""
        max_rows = min(20, len(raw_df))
        for i in range(max_rows):
            norm_vals = [_normalize_key(v) for v in raw_df.iloc[i].tolist()]
            has_gstin = any("gstin" in v for v in norm_vals)
            has_invno = any(("invoiceno" in v or "invoicenumber" in v) for v in norm_vals)
            if has_gstin and has_invno:
                return i
        return None

    @staticmethod
    def parse_excel(file_bytes: bytes) -> List[Dict[str, Any]]:
        """Parses GSTR-2B/purchase data from an Excel (.xlsx/.xls) file.
        Handles multi-sheet workbooks (e.g. official GSTR-2B export with a
        'B2B' sheet) and header rows that aren't on row 1 (junk/title rows
        above, common in both GSTR-2B and Tally exports)."""
        try:
            import pandas as pd
            from io import BytesIO
        except ImportError:
            return []

        xls = None
        for engine in ("openpyxl", None):
            try:
                xls = pd.ExcelFile(BytesIO(file_bytes), engine=engine) if engine else pd.ExcelFile(BytesIO(file_bytes))
                break
            except Exception:
                continue
        if xls is None:
            return []

        df = None
        for sheet in xls.sheet_names:
            try:
                raw = xls.parse(sheet, header=None)
            except Exception:
                continue
            header_idx = GSTR2BParser._find_header_row(raw)
            if header_idx is None:
                continue
            try:
                candidate = xls.parse(sheet, header=header_idx)
            except Exception:
                continue
            candidate.columns = [normalize_headers(candidate.columns)[c] for c in candidate.columns]
            if "invoice_number" in candidate.columns and "supplier_gstin" in candidate.columns:
                df = candidate
                break

        if df is None or df.empty:
            return []

        try:
            df = df.dropna(subset=["invoice_number", "supplier_gstin"])
            df = _derive_amounts(df)

            records = df.to_dict(orient="records")
            cleaned = []
            for r in records:
                row = {}
                for k, v in r.items():
                    if isinstance(v, float) and k not in ("tax_amount", "taxable_amount"):
                        v = str(int(v)) if v.is_integer() else str(v)
                    elif not isinstance(v, (int, float)):
                        v = str(v).strip()
                    row[k] = v
                cleaned.append(row)
            return cleaned
        except Exception:
            return []


class GSTRMatchingEngine:
    @staticmethod
    def match_invoice(purchase_invoice: Dict[str, Any], gstr2b_invoices: List[Dict[str, Any]]) -> MatchResult:
        """Matches a single purchase invoice against GSTR-2B records."""
        inv_no = purchase_invoice.get("invoice_number")
        sup_gstin = purchase_invoice.get("supplier_gstin")

        matched_rec = None
        for g2b in gstr2b_invoices:
            if g2b.get("invoice_number") == inv_no and g2b.get("supplier_gstin") == sup_gstin:
                matched_rec = g2b
                break

        # Fallback: fuzzy match on invoice number (handles FY-prefixed formats
        # like "26-27/0032" on the portal vs "32" in accounting software) as
        # long as the supplier GSTIN matches exactly.
        if not matched_rec:
            inv_key = _invoice_number_key(inv_no)
            for g2b in gstr2b_invoices:
                if g2b.get("supplier_gstin") == sup_gstin and _invoice_number_key(g2b.get("invoice_number")) == inv_key:
                    matched_rec = g2b
                    break

        if not matched_rec:
            return MatchResult(
                invoice_number=inv_no,
                supplier_gstin=sup_gstin,
                status=MatchStatus.MISSING_IN_GSTR2B,
                discrepancies=["Invoice missing in GSTR-2B portal data."]
            )

        try:
            purchase_tax = float(purchase_invoice.get("tax_amount", 0.0))
            portal_tax = float(matched_rec.get("tax_amount", 0.0))
        except (TypeError, ValueError):
            purchase_tax, portal_tax = 0.0, 0.0

        tax_diff = round(abs(purchase_tax - portal_tax), 2)

        discrepancies = []
        if tax_diff > 0:
            discrepancies.append(f"Tax amount mismatch: Purchase={purchase_tax}, Portal={portal_tax}")

        status = MatchStatus.MISMATCHED if discrepancies else MatchStatus.MATCHED

        return MatchResult(
            invoice_number=inv_no,
            supplier_gstin=sup_gstin,
            status=status,
            discrepancies=discrepancies,
            tax_diff=tax_diff,
            matched_data=matched_rec
        )

    @staticmethod
    def run_matching(purchase_invoices: List[Dict[str, Any]], gstr2b_invoices: List[Dict[str, Any]]) -> List[MatchResult]:
        """Runs bulk matching for all purchase invoices against GSTR-2B."""
        results = []
        for inv in purchase_invoices:
            results.append(GSTRMatchingEngine.match_invoice(inv, gstr2b_invoices))
        return results
