"""
gstr_matching.py
================
GSTR-2B Matching Engine for AI Tax & Document Audit Assistant
"""
import csv
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any, List, Dict


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
        """Parses GSTR-2B data from CSV format."""
        invoices = []
        try:
            reader = csv.DictReader(file_content.splitlines())
            for row in reader:
                if row.get("supplier_gstin"):
                    invoices.append(row)
        except Exception:
            pass
        return invoices

    @staticmethod
    def parse_excel(file_bytes: bytes) -> List[Dict[str, Any]]:
        """Parses GSTR-2B/purchase data from an Excel (.xlsx/.xls) file."""
        try:
            import pandas as pd
            from io import BytesIO
        except ImportError:
            return []

        df = None
        for engine in ("openpyxl", None):
            try:
                df = pd.read_excel(BytesIO(file_bytes), engine=engine) if engine else pd.read_excel(BytesIO(file_bytes))
                break
            except Exception:
                continue

        if df is None or df.empty:
            return []

        try:
            df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
            if "invoice_number" not in df.columns or "supplier_gstin" not in df.columns:
                return []

            df = df.dropna(subset=["invoice_number", "supplier_gstin"])
            if "tax_amount" in df.columns:
                df["tax_amount"] = pd.to_numeric(df["tax_amount"], errors="coerce").fillna(0.0)

            records = df.to_dict(orient="records")
            cleaned = []
            for r in records:
                row = {}
                for k, v in r.items():
                    if isinstance(v, float) and k != "tax_amount":
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
