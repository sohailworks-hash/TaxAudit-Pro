"""
gst_validator.py
================
GST Validation Engine for AI Tax & Document Audit Assistant - Step 3
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class FlagSeverity(str, Enum):
    RED = "RED"
    YELLOW = "YELLOW"
    GREEN = "GREEN"


class TransactionType(str, Enum):
    INTRA_STATE = "INTRA_STATE"
    INTER_STATE = "INTER_STATE"
    UNKNOWN = "UNKNOWN"


GSTIN_REGEX = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana",
    "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory",
    "99": "Centre Jurisdiction",
}

GSTIN_CHAR_MAP = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class ValidationFlag:
    field: str
    severity: FlagSeverity
    message: str
    expected: Optional[str] = None
    found: Optional[str] = None
    rule_code: Optional[str] = None


@dataclass
class GSTValidationResult:
    is_valid: bool
    gstin: str
    state_code: Optional[str] = None
    state_name: Optional[str] = None
    flags: list[ValidationFlag] = field(default_factory=list)
    pan: Optional[str] = None
    overall_severity: str = "GREEN"
    is_valid_format: bool = True
    transaction_type: TransactionType = TransactionType.UNKNOWN
    expected_tax_type: Optional[str] = None

    def __post_init__(self):
        if any(f.severity == FlagSeverity.RED for f in self.flags):
            self.overall_severity = "RED"
        elif any(f.severity == FlagSeverity.YELLOW for f in self.flags):
            self.overall_severity = "YELLOW"
        else:
            self.overall_severity = "GREEN"


def validate_gstin_format(gstin: str) -> bool:
    if not gstin or not isinstance(gstin, str):
        return False
    if any(c.islower() for c in gstin):
        return False
    return bool(re.match(GSTIN_REGEX, gstin.strip().upper()))


def verify_gstin_checksum(gstin: str) -> bool:
    """Standard Mod-36 checksum verification for GSTIN."""
    if not gstin or not isinstance(gstin, str):
        return False
    gstin = gstin.strip().upper()
    if not re.match(GSTIN_REGEX, gstin):
        return False

    factor = 1
    total_sum = 0
    modulus = len(GSTIN_CHAR_MAP)
    chars = gstin[:-1]
    given_checksum = gstin[-1]

    for char in chars:
        code_point = GSTIN_CHAR_MAP.find(char)
        if code_point == -1:
            return False
        digit = code_point * factor
        digit = (digit // modulus) + (digit % modulus)
        total_sum += digit
        factor = 2 if factor == 1 else 1

    rem = total_sum % modulus
    check_code = (modulus - rem) % modulus
    expected_char = GSTIN_CHAR_MAP[check_code]

    return expected_char == given_checksum


def get_state_info(gstin: str) -> tuple[Optional[str], Optional[str]]:
    if not gstin or not isinstance(gstin, str) or len(gstin.strip()) < 2:
        return None, None
    code = gstin.strip()[:2]
    if code not in STATE_CODES:
        return None, None
    return code, STATE_CODES.get(code)


def extract_state_code(gstin: str) -> Optional[str]:
    if not gstin or not isinstance(gstin, str) or len(gstin.strip()) < 2:
        return None
    code = gstin.strip()[:2]
    if code not in STATE_CODES:
        return None
    return code


def determine_transaction_type(supplier_state: Optional[str], buyer_state: Optional[str]) -> TransactionType:
    if not supplier_state or not buyer_state:
        return TransactionType.UNKNOWN
    if supplier_state not in STATE_CODES or buyer_state not in STATE_CODES:
        return TransactionType.UNKNOWN
    if supplier_state == buyer_state:
        return TransactionType.INTRA_STATE
    return TransactionType.INTER_STATE


def gst_flags_to_db_format(flags: Any, invoice_id: Optional[str] = None) -> list[dict[str, Any]]:
    if hasattr(flags, "flags"):
        flag_list = flags.flags
    elif isinstance(flags, list):
        flag_list = flags
    else:
        flag_list = []

    serialized = []
    for flag in flag_list:
        item = {
            "field": flag.field,
            "severity": flag.severity.value if isinstance(flag.severity, FlagSeverity) else flag.severity,
            "message": flag.message,
            "expected": flag.expected,
            "found": flag.found,
            "rule_code": flag.rule_code,
            "source": "gst_validator"
        }
        if invoice_id:
            item["invoice_id"] = invoice_id
        serialized.append(item)
    return serialized


def validate_invoice_gst(
    supplier_gstin: str,
    buyer_gstin: Optional[str] = None,
    seller_state_code: Optional[str] = None,
    buyer_state_code: Optional[str] = None,
    place_of_supply_code: Optional[str] = None,
    taxable_amount: float = 0.0,
    cgst: float = 0.0,
    sgst: float = 0.0,
    igst: float = 0.0,
    cgst_amount: Optional[float] = None,
    sgst_amount: Optional[float] = None,
    igst_amount: Optional[float] = None,
    invoice_number: Optional[str] = None,
    item_tax_rate: float = 18.0,
) -> GSTValidationResult:
    flags = []

    effective_cgst = cgst_amount if cgst_amount is not None else cgst
    effective_sgst = sgst_amount if sgst_amount is not None else sgst
    effective_igst = igst_amount if igst_amount is not None else igst

    sup_clean = (supplier_gstin or "").strip().upper()
    is_valid_fmt = validate_gstin_format(supplier_gstin)
    is_valid = True

    supplier_state = seller_state_code or (sup_clean[:2] if len(sup_clean) >= 2 else None)

    if not is_valid_fmt:
        is_valid = False
        flags.append(
            ValidationFlag(
                field="supplier_gstin",
                severity=FlagSeverity.RED,
                message="Supplier GSTIN format is invalid.",
                found=supplier_gstin,
                rule_code="GST_001"
            )
        )
    elif not verify_gstin_checksum(sup_clean):
        is_valid = False
        flags.append(
            ValidationFlag(
                field="supplier_gstin",
                severity=FlagSeverity.RED,
                message="Supplier GSTIN checksum validation failed.",
                found=supplier_gstin,
                rule_code="GST_002"
            )
        )

    effective_buyer_state = buyer_state_code or place_of_supply_code
    if not effective_buyer_state and buyer_gstin:
        buy_clean = (buyer_gstin or "").strip().upper()
        if len(buy_clean) >= 2:
            effective_buyer_state = buy_clean[:2]

    tx_type = determine_transaction_type(supplier_state, effective_buyer_state)
    if tx_type == TransactionType.UNKNOWN and supplier_state and effective_buyer_state:
        if supplier_state == effective_buyer_state:
            tx_type = TransactionType.INTRA_STATE
        else:
            tx_type = TransactionType.INTER_STATE

    if supplier_state and effective_buyer_state:
        if supplier_state == effective_buyer_state:
            if effective_igst > 0:
                flags.append(
                    ValidationFlag(
                        field="igst",
                        severity=FlagSeverity.RED,
                        message="Intra-state transaction cannot have IGST charges.",
                        expected="0.0",
                        found=str(effective_igst),
                        rule_code="GST_005"
                    )
                )

            if effective_cgst > 0 and effective_sgst > 0:
                if effective_cgst != effective_sgst:
                    flags.append(
                        ValidationFlag(
                            field="cgst_sgst",
                            severity=FlagSeverity.YELLOW,
                            message="CGST and SGST amounts must be equal.",
                            expected=str(effective_cgst),
                            found=f"CGST: {effective_cgst}, SGST: {effective_sgst}",
                            rule_code="GST_007"
                        )
                    )
                elif taxable_amount > 0:
                    expected_cgst = round((taxable_amount * (item_tax_rate / 2)) / 100, 2)
                    expected_sgst = expected_cgst
                    if abs(effective_cgst - expected_cgst) > 5.0 or abs(effective_sgst - expected_sgst) > 5.0:
                        flags.append(
                            ValidationFlag(
                                field="cgst_sgst",
                                severity=FlagSeverity.YELLOW,
                                message="CGST and SGST amounts deviate from expected calculation.",
                                expected=str(expected_cgst),
                                found=f"CGST: {effective_cgst}, SGST: {effective_sgst}",
                                rule_code="GST_007"
                            )
                        )
        else:
            if effective_cgst > 0 or effective_sgst > 0:
                flags.append(
                    ValidationFlag(
                        field="cgst_sgst",
                        severity=FlagSeverity.RED,
                        message="Inter-state transaction cannot have CGST or SGST charges.",
                        expected="0.0",
                        found=f"CGST: {effective_cgst}, SGST: {effective_sgst}",
                        rule_code="GST_008"
                    )
                )

            if taxable_amount > 0:
                expected_igst = round((taxable_amount * item_tax_rate) / 100, 2)
                if abs(effective_igst - expected_igst) > 5.0:
                    flags.append(
                        ValidationFlag(
                            field="igst",
                            severity=FlagSeverity.YELLOW,
                            message="IGST amount deviates from expected calculation.",
                            expected=str(expected_igst),
                            found=str(effective_igst),
                            rule_code="GST_006"
                        )
                    )

    state_code, state_name = get_state_info(sup_clean)
    pan = sup_clean[2:12] if len(sup_clean) >= 12 else None

    if tx_type == TransactionType.INTRA_STATE:
        expected_tax_type = "CGST + SGST"
    elif tx_type == TransactionType.INTER_STATE:
        expected_tax_type = "IGST"
    else:
        expected_tax_type = None

    return GSTValidationResult(
        is_valid=is_valid and len([f for f in flags if f.severity == FlagSeverity.RED]) == 0,
        gstin=supplier_gstin,
        state_code=state_code,
        state_name=state_name,
        flags=flags,
        pan=pan,
        is_valid_format=is_valid_fmt,
        transaction_type=tx_type,
        expected_tax_type=expected_tax_type
    )


class GSTValidator:
    @staticmethod
    def validate_format(gstin: str) -> bool:
        return validate_gstin_format(gstin)

    @staticmethod
    def verify_checksum(gstin: str) -> bool:
        return verify_gstin_checksum(gstin)

    @staticmethod
    def validate_invoice(
        supplier_gstin: str,
        buyer_gstin: Optional[str] = None,
        seller_state_code: Optional[str] = None,
        buyer_state_code: Optional[str] = None,
        place_of_supply_code: Optional[str] = None,
        taxable_amount: float = 0.0,
        cgst: float = 0.0,
        sgst: float = 0.0,
        igst: float = 0.0,
        cgst_amount: Optional[float] = None,
        sgst_amount: Optional[float] = None,
        igst_amount: Optional[float] = None,
        invoice_number: Optional[str] = None,
        item_tax_rate: float = 18.0,
    ) -> GSTValidationResult:
        return validate_invoice_gst(
            supplier_gstin=supplier_gstin,
            buyer_gstin=buyer_gstin,
            seller_state_code=seller_state_code,
            buyer_state_code=buyer_state_code,
            place_of_supply_code=place_of_supply_code,
            taxable_amount=taxable_amount,
            cgst=cgst,
            sgst=sgst,
            igst=igst,
            cgst_amount=cgst_amount,
            sgst_amount=sgst_amount,
            igst_amount=igst_amount,
            invoice_number=invoice_number,
            item_tax_rate=item_tax_rate,
        )


_validate_gstin_format = validate_gstin_format
_validate_gstin_checksum = verify_gstin_checksum
_extract_state_code = extract_state_code
_get_state_info = get_state_info
_determine_transaction_type = determine_transaction_type