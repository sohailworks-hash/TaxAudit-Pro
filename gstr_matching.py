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
    remapped = {mapping[k]: v for k, v in row.items()}
    for gk in ("supplier_gstin", "buyer_gstin"):
        if remapped.get(gk):
            remapped[gk] = str(remapped[gk]).strip().upper()
    return remapped


GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"(?:₹|Rs\.?)?\s?([\d,]+\.\d{1,2}|\d{2,})")
INV_HINT_RE = re.compile(r"(?:inv(?:oice)?\.?\s*(?:no|#|num)?\.?\s*[:\-]?\s*)([A-Za-z0-9\-/]+)", re.I)


def parse_raw_text(text: str) -> List[dict]:
    """Extracts invoice_number, supplier_gstin, tax_amount from freeform pasted
    text (e.g. copied from GST portal, WhatsApp, Tally screen, or a raw CSV
    dump). Handles two layouts:
      - One record per line (CSV rows, portal table copy-paste): each line
        with its own GSTIN becomes its own record.
      - One record spread across a multi-line block ("GSTIN: ...",
        "Invoice: ...", "Amount: ..." on separate lines, blocks separated
        by a blank line): the whole block is treated as one record.
    Best-effort — always review before use."""
    records = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip()]
        gstin_line_idxs = [i for i, l in enumerate(lines) if GSTIN_RE.search(l)]

        # 0 or 1 GSTIN in the block -> whole block is one record (multi-line
        # single-record layout). >1 -> one record per GSTIN-bearing line
        # (row-per-record layout, e.g. pasted CSV).
        candidates = [block] if len(gstin_line_idxs) <= 1 else [lines[i] for i in gstin_line_idxs]

        for line in candidates:
            gstin_match = GSTIN_RE.search(line)
            if not gstin_match:
                continue
            gstin = gstin_match.group(0).strip().upper()
            inv_match = INV_HINT_RE.search(line)
            invoice_number = inv_match.group(1) if inv_match else None
            if not invoice_number:
                # fallback: first standalone token before/near GSTIN that looks like an invoice id
                pre = line[: gstin_match.start()]
                tok = re.findall(r"[A-Za-z0-9\-/]+", pre)
                invoice_number = tok[-1] if tok else None
            amounts_text = line[:gstin_match.start()] + line[gstin_match.end():]
            amounts = [float(a.replace(",", "")) for a in AMOUNT_RE.findall(amounts_text)]
            tax_amount = max(amounts) if amounts else None
            rec = {"invoice_number": invoice_number, "supplier_gstin": gstin}
            if tax_amount is not None:
                rec["tax_amount"] = tax_amount
            if rec not in records:
                records.append(rec)
    return records


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
    """Normalizes an invoice number for matching so real-world portal formats
    ('26-27/0032', 'T00137/26-27', '019326-27' [serial+FY glued with no
    separator], 'SHP/149/2026-27', 'SINV-26-02244', 'AHD/002540/SI26') and
    plain Tally serials ('0032', '32') all resolve to the same key.
    Strategy: strip an exact trailing financial-year suffix ('YY-YY' or
    'YYYY-YY') if present, then take the LONGEST remaining digit run (the
    invoice serial is always longer than a 2-digit FY/branch code) rather
    than the last one."""
    if inv is None:
        return ""
    s = str(inv).strip()
    core = s
    if len(core) >= 5 and re.fullmatch(r"\d{2}-\d{2}", core[-5:]):
        core = core[:-5]
    elif len(core) >= 7 and re.fullmatch(r"\d{4}-\d{2}", core[-7:]):
        core = core[:-7]
    digits_groups = re.findall(r"\d+", core) or re.findall(r"\d+", s)
    if digits_groups:
        best = max(enumerate(digits_groups), key=lambda x: (len(x[1]), x[0]))[1]  # longest; last on tie
        return str(int(best))
    return s.lower()


# Anything within ₹2 is treated as normal rounding, not a real mismatch.
TAX_DIFF_TOLERANCE = 2.0


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

        all_cleaned = []
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
            if "invoice_number" not in candidate.columns or "supplier_gstin" not in candidate.columns:
                continue

            try:
                df = candidate.dropna(subset=["invoice_number", "supplier_gstin"])
                if df.empty:
                    continue
                df = _derive_amounts(df)

                records = df.to_dict(orient="records")
                for r in records:
                    row = {}
                    for k, v in r.items():
                        if isinstance(v, float) and k not in ("tax_amount", "taxable_amount"):
                            v = str(int(v)) if v.is_integer() else str(v)
                        elif not isinstance(v, (int, float)):
                            v = str(v).strip()
                        row[k] = v
                    if "supplier_gstin" in row:
                        row["supplier_gstin"] = str(row["supplier_gstin"]).strip().upper()
                    if "buyer_gstin" in row:
                        row["buyer_gstin"] = str(row["buyer_gstin"]).strip().upper()
                    all_cleaned.append(row)
            except Exception:
                continue

        return all_cleaned


def _norm_gstin(g) -> str:
    return str(g).strip().upper() if g else ""


class GSTRMatchingEngine:
    @staticmethod
    def match_invoice(purchase_invoice: Dict[str, Any], gstr2b_invoices: List[Dict[str, Any]],
                       used_ids: Optional[set] = None) -> MatchResult:
        """Matches a single purchase invoice against GSTR-2B records.
        used_ids (optional): set of id(g2b_record) already consumed by an
        earlier purchase invoice in this run, so one portal record can't be
        matched twice (double-count) when passed via run_matching()."""
        inv_no = purchase_invoice.get("invoice_number")
        sup_gstin = _norm_gstin(purchase_invoice.get("supplier_gstin"))
        if used_ids is None:
            used_ids = set()

        matched_rec = None
        for g2b in gstr2b_invoices:
            if id(g2b) in used_ids:
                continue
            if g2b.get("invoice_number") == inv_no and _norm_gstin(g2b.get("supplier_gstin")) == sup_gstin:
                matched_rec = g2b
                break

        # Fallback: fuzzy match on invoice number (handles FY-prefixed formats
        # like "26-27/0032" on the portal vs "32" in accounting software) as
        # long as the supplier GSTIN matches exactly. If more than one record
        # shares the same normalized number (e.g. same serial reused across
        # years), pick the one whose tax amount is closest to the purchase
        # invoice's tax amount to avoid a wrong-year false match.
        if not matched_rec:
            inv_key = _invoice_number_key(inv_no)
            candidates = [
                g2b for g2b in gstr2b_invoices
                if id(g2b) not in used_ids and _norm_gstin(g2b.get("supplier_gstin")) == sup_gstin
                and _invoice_number_key(g2b.get("invoice_number")) == inv_key
            ]
            if len(candidates) == 1:
                matched_rec = candidates[0]
            elif len(candidates) > 1:
                try:
                    target_tax = float(purchase_invoice.get("tax_amount", 0.0))
                except (TypeError, ValueError):
                    target_tax = 0.0
                def _tax_gap(c):
                    try:
                        return abs(float(c.get("tax_amount", 0.0)) - target_tax)
                    except (TypeError, ValueError):
                        return float("inf")
                matched_rec = min(candidates, key=_tax_gap)

        # Last-resort fallback: same GSTIN + exact tax amount match, for
        # cases where invoice-number formats differ too much to normalize
        # (e.g. purchase '499' vs portal 'SWT2627-499'). Only used when
        # exactly one such candidate exists, to avoid wrong matches.
        if not matched_rec:
            try:
                target_tax = round(float(purchase_invoice.get("tax_amount", 0.0)), 2)
            except (TypeError, ValueError):
                target_tax = None
            if target_tax is not None:
                amount_candidates = []
                for g2b in gstr2b_invoices:
                    if id(g2b) in used_ids or _norm_gstin(g2b.get("supplier_gstin")) != sup_gstin:
                        continue
                    try:
                        if round(float(g2b.get("tax_amount", 0.0)), 2) == target_tax:
                            amount_candidates.append(g2b)
                    except (TypeError, ValueError):
                        continue
                if len(amount_candidates) == 1:
                    matched_rec = amount_candidates[0]

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
        if tax_diff > TAX_DIFF_TOLERANCE:
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
        """Runs bulk matching for all purchase invoices against GSTR-2B.
        Tracks consumed GSTR-2B records so one portal invoice can't be
        matched to two different purchase invoices."""
        results = []
        used_ids: set = set()
        for inv in purchase_invoices:
            r = GSTRMatchingEngine.match_invoice(inv, gstr2b_invoices, used_ids)
            if r.matched_data:
                used_ids.add(id(r.matched_data))
            results.append(r)
        return results
