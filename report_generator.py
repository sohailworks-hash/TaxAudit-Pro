from io import BytesIO
from datetime import datetime, timedelta, timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

IST = timezone(timedelta(hours=5, minutes=30))
BRAND_DARK = colors.HexColor("#101820")
BRAND_ACCENT = colors.HexColor("#C79A3A")
FOOTER_CONTACT = "Sohail Studio · GST Audit Assistant · sohailkhan902314@gmail.com · +91-8955377472"

SEV_COLORS = {
    "GREEN": colors.HexColor("#1E6B4E"),
    "YELLOW": colors.HexColor("#9A6A00"),
    "RED": colors.HexColor("#B5311E"),
}
MATCH_COLORS = {
    "MATCHED": colors.HexColor("#1E6B4E"),
    "MISMATCHED": colors.HexColor("#9A6A00"),
    "MISSING_IN_GSTR2B": colors.HexColor("#B5311E"),
}


def _letterhead(canvas, doc):
    canvas.saveState()
    # top color band
    canvas.setFillColor(BRAND_DARK)
    canvas.rect(0, A4[1] - 16 * mm, A4[0], 16 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(20 * mm, A4[1] - 11 * mm, "GST Audit Assistant")
    canvas.setFillColor(BRAND_ACCENT)
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(A4[0] - 20 * mm, A4[1] - 11 * mm, "Powered by Sohail Studio")
    # footer
    canvas.setFillColor(colors.HexColor("#7A7263"))
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(20 * mm, 12 * mm, FOOTER_CONTACT)
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(colors.HexColor("#DFDACD"))
    canvas.line(20 * mm, 15 * mm, A4[0] - 20 * mm, 15 * mm)
    canvas.restoreState()


def _base_doc():
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=24 * mm, bottomMargin=22 * mm)
    return buf, doc


def _summary_table(headers, values):
    data = [headers, [str(v) for v in values]]
    t = Table(data, colWidths=[110] * len(headers))
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DFDACD")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def generate_audit_pdf(firm_name: str, client_name: str, results: list, summary: dict) -> bytes:
    buf, doc = _base_doc()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], textColor=colors.grey, spaceAfter=14)

    elems = [
        Paragraph(firm_name or "GST Invoice Audit Report", title_style),
        Paragraph(
            f"Client: {client_name or '—'} · Generated: {datetime.now(IST).strftime('%d-%b-%Y %H:%M')} IST",
            sub_style,
        ),
        _summary_table(
            ["Total", "Green", "Yellow", "Red"],
            [summary.get("total", 0), summary.get("green", 0), summary.get("yellow", 0), summary.get("red", 0)],
        ),
        Spacer(1, 16), Paragraph("Invoice Details", styles["Heading2"]), Spacer(1, 6),
    ]

    rows = [["Invoice #", "GSTIN", "Status", "Type", "Flags"]]
    for r in results:
        flags_text = "; ".join(f.get("message", "") for f in r.get("flags", [])) or "No issues"
        rows.append([
            r.get("invoice_number") or "—",
            r.get("gstin", ""),
            r.get("overall_severity", ""),
            r.get("transaction_type", ""),
            Paragraph(flags_text, styles["Normal"]),
        ])

    tbl = Table(rows, colWidths=[65, 110, 55, 75, 175], repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DFDACD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i, r in enumerate(results, start=1):
        style_cmds.append(("TEXTCOLOR", (2, i), (2, i), SEV_COLORS.get(r.get("overall_severity", ""), colors.black)))
    tbl.setStyle(TableStyle(style_cmds))
    elems.append(tbl)

    doc.build(elems, onFirstPage=_letterhead, onLaterPages=_letterhead)
    return buf.getvalue()


def generate_match_summary_pdf(firm_name: str, client_name: str, results: list, summary: dict) -> bytes:
    buf, doc = _base_doc()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], textColor=colors.grey, spaceAfter=14)

    elems = [
        Paragraph(firm_name or "GSTR-2B Match Report", title_style),
        Paragraph(
            f"Client: {client_name or '—'} · Generated: {datetime.now(IST).strftime('%d-%b-%Y %H:%M')} IST",
            sub_style,
        ),
        _summary_table(
            ["Total", "Matched", "Mismatched", "Missing"],
            [summary.get("total", 0), summary.get("matched", 0),
             summary.get("mismatched", 0), summary.get("missing_in_gstr2b", 0)],
        ),
        Spacer(1, 16), Paragraph("Match Details", styles["Heading2"]), Spacer(1, 6),
    ]

    rows = [["Invoice #", "GSTIN", "Status", "Tax Diff", "Discrepancies"]]
    for r in results:
        disc_text = "; ".join(r.get("discrepancies", []) or []) or "None"
        rows.append([
            r.get("invoice_number") or "—",
            r.get("supplier_gstin", "") or "—",
            (r.get("status", "") or "").replace("_", " "),
            f"Rs.{r.get('tax_diff', 0)}" if r.get("tax_diff") else "—",
            Paragraph(disc_text, styles["Normal"]),
        ])

    tbl = Table(rows, colWidths=[65, 110, 70, 60, 175], repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DFDACD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i, r in enumerate(results, start=1):
        style_cmds.append(("TEXTCOLOR", (2, i), (2, i), MATCH_COLORS.get(r.get("status", ""), colors.black)))
    tbl.setStyle(TableStyle(style_cmds))
    elems.append(tbl)

    doc.build(elems, onFirstPage=_letterhead, onLaterPages=_letterhead)
    return buf.getvalue()
