from io import BytesIO
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

SEV_COLORS = {
    "GREEN": colors.HexColor("#1E6B4E"),
    "YELLOW": colors.HexColor("#9A6A00"),
    "RED": colors.HexColor("#B5311E"),
}


def generate_audit_pdf(firm_name: str, client_name: str, results: list, summary: dict) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Heading1"], fontSize=18, spaceAfter=4)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], textColor=colors.grey, spaceAfter=14)

    elems = [
        Paragraph(firm_name or "GST Audit Assistant", title_style),
        Paragraph(
            f"Client: {client_name or '—'} · Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}",
            sub_style,
        ),
    ]

    summary_data = [
        ["Total", "Green", "Yellow", "Red"],
        [
            str(summary.get("total", 0)),
            str(summary.get("green", 0)),
            str(summary.get("yellow", 0)),
            str(summary.get("red", 0)),
        ],
    ]
    st = Table(summary_data, colWidths=[110] * 4)
    st.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#101820")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DFDACD")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    elems += [st, Spacer(1, 16), Paragraph("Invoice Details", styles["Heading2"]), Spacer(1, 6)]

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
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#101820")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DFDACD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i, r in enumerate(results, start=1):
        style_cmds.append(
            ("TEXTCOLOR", (2, i), (2, i), SEV_COLORS.get(r.get("overall_severity", ""), colors.black))
        )
    tbl.setStyle(TableStyle(style_cmds))
    elems.append(tbl)

    doc.build(elems)
    return buf.getvalue()
