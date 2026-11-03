"""
Compliance Certificate PDF Generation Module

Generates professional PDF reports for sanctions screening results.
Uses ReportLab for PDF creation with a formal letterhead layout.
"""

import hashlib
import io
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# Color scheme
NAVY = colors.HexColor("#1a365d")
LIGHT_GRAY = colors.HexColor("#f8fafc")
BORDER_GRAY = colors.HexColor("#e2e8f0")
SUCCESS_GREEN = colors.HexColor("#10b981")
WARNING_RED = colors.HexColor("#dc2626")
TEXT_DARK = colors.HexColor("#1e293b")
TEXT_MUTED = colors.HexColor("#64748b")


def compute_file_hash(file_content: bytes) -> str:
    """Compute SHA-256 hash of file content."""
    return hashlib.sha256(file_content).hexdigest()


def generate_compliance_certificate(
    results: dict,
    file_hash: str,
    metadata: Optional[dict] = None,
    organization_name: str = "Sanctions Screening Service",
) -> bytes:
    """
    Generate a Compliance Certificate PDF.

    Args:
        results: Screening results dict containing:
            - total_screened: int
            - total_matches: int
            - high_confidence_count: int
            - potential_match_count: int
            - clear_count: int
            - flagged_results: list[dict]
        file_hash: SHA-256 hash of the uploaded file
        metadata: Optional dict with matter_number, role, etc.
        organization_name: Name to display in header

    Returns:
        PDF file content as bytes
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )

    # Build styles
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="Header",
        fontSize=24,
        fontName="Helvetica-Bold",
        textColor=NAVY,
        alignment=TA_CENTER,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="SubHeader",
        fontSize=12,
        fontName="Helvetica",
        textColor=TEXT_MUTED,
        alignment=TA_CENTER,
        spaceAfter=20,
    ))
    styles.add(ParagraphStyle(
        name="SectionTitle",
        fontSize=14,
        fontName="Helvetica-Bold",
        textColor=NAVY,
        spaceBefore=20,
        spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        name="BodyText",
        fontSize=10,
        fontName="Helvetica",
        textColor=TEXT_DARK,
        leading=14,
    ))
    styles.add(ParagraphStyle(
        name="SmallText",
        fontSize=8,
        fontName="Helvetica",
        textColor=TEXT_MUTED,
        leading=10,
    ))
    styles.add(ParagraphStyle(
        name="CertificateNumber",
        fontSize=10,
        fontName="Helvetica",
        textColor=TEXT_MUTED,
        alignment=TA_RIGHT,
    ))
    styles.add(ParagraphStyle(
        name="StatusClear",
        fontSize=16,
        fontName="Helvetica-Bold",
        textColor=SUCCESS_GREEN,
        alignment=TA_CENTER,
        spaceBefore=10,
        spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        name="StatusAlert",
        fontSize=16,
        fontName="Helvetica-Bold",
        textColor=WARNING_RED,
        alignment=TA_CENTER,
        spaceBefore=10,
        spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        name="HashText",
        fontSize=8,
        fontName="Courier",
        textColor=TEXT_DARK,
        alignment=TA_CENTER,
        leading=12,
    ))

    # Build document content
    story = []
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    certificate_id = file_hash[:16].upper()

    # Header / Letterhead
    story.append(Paragraph(organization_name, styles["Header"]))
    story.append(Paragraph("Sanctions Screening Compliance Certificate", styles["SubHeader"]))
    story.append(HRFlowable(width="100%", thickness=2, color=NAVY, spaceBefore=0, spaceAfter=20))

    # Certificate metadata
    story.append(Paragraph(f"Certificate ID: {certificate_id}", styles["CertificateNumber"]))
    story.append(Paragraph(f"Generated: {timestamp}", styles["CertificateNumber"]))
    story.append(Spacer(1, 20))

    # Screening Summary Section
    story.append(Paragraph("Summary of Results", styles["SectionTitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=15))

    total_screened = results.get("total_screened", 0)
    total_matches = results.get("total_matches", 0)
    high_confidence = results.get("high_confidence_count", 0)
    potential_matches = results.get("potential_match_count", 0)
    clear_count = results.get("clear_count", 0)

    # Status indicator
    if total_matches == 0:
        story.append(Paragraph("NO MATCHES FOUND", styles["StatusClear"]))
        story.append(Paragraph(
            "All screened individuals have been checked against UN, SEMA, and "
            "Public Safety Canada sanctions lists with no potential matches identified.",
            styles["BodyText"]
        ))
    else:
        story.append(Paragraph("MATCHES REQUIRE REVIEW", styles["StatusAlert"]))
        story.append(Paragraph(
            f"Screening identified {total_matches} potential match(es) requiring human review. "
            "See detailed findings below.",
            styles["BodyText"]
        ))

    story.append(Spacer(1, 15))

    # Summary statistics table
    summary_data = [
        ["Metric", "Count"],
        ["Total Names Screened", str(total_screened)],
        ["Cleared (No Match)", str(clear_count)],
        ["High Confidence Matches", str(high_confidence)],
        ["Potential Matches", str(potential_matches)],
    ]

    summary_table = Table(summary_data, colWidths=[3.5 * inch, 1.5 * inch])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (-1, -1), LIGHT_GRAY),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(summary_table)

    # Flagged Results Detail (if any)
    flagged_results = results.get("flagged_results", [])
    if flagged_results:
        story.append(Spacer(1, 20))
        story.append(Paragraph("Flagged Matches Detail", styles["SectionTitle"]))
        story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=15))

        # Build matches table
        matches_header = ["Client Name", "Matched Name", "Score", "Category", "Source"]
        matches_data = [matches_header]

        for match in flagged_results[:20]:  # Limit to 20 for PDF readability
            matches_data.append([
                match.get("client_name", "N/A")[:30],
                match.get("matched_name", "N/A")[:30],
                f"{match.get('score', 0)}%",
                match.get("category", "N/A"),
                _get_source_abbrev(match.get("source_list", "")),
            ])

        if len(flagged_results) > 20:
            matches_data.append(["...", f"({len(flagged_results) - 20} more)", "", "", ""])

        matches_table = Table(
            matches_data,
            colWidths=[1.5 * inch, 1.5 * inch, 0.7 * inch, 1.2 * inch, 0.8 * inch]
        )
        matches_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), WARNING_RED),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (2, 0), (2, -1), "CENTER"),
            ("ALIGN", (3, 0), (4, -1), "CENTER"),
            ("BACKGROUND", (0, 1), (-1, -1), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
        ]))
        story.append(matches_table)

    # Metadata section (if provided)
    if metadata:
        story.append(Spacer(1, 20))
        story.append(Paragraph("File Metadata", styles["SectionTitle"]))
        story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=15))

        meta_data = [["Field", "Value"]]
        for key, value in metadata.items():
            display_key = key.replace("_", " ").title()
            meta_data.append([display_key, str(value)])

        meta_table = Table(meta_data, colWidths=[2 * inch, 4 * inch])
        meta_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("BACKGROUND", (0, 1), (-1, -1), LIGHT_GRAY),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(meta_table)

    # Digital Fingerprint Section
    story.append(Spacer(1, 30))
    story.append(Paragraph("Digital Fingerprint", styles["SectionTitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=15))

    story.append(Paragraph(
        "This certificate is digitally signed using the SHA-256 hash of the original uploaded file. "
        "This fingerprint can be used to verify the authenticity and integrity of the screening results.",
        styles["BodyText"]
    ))
    story.append(Spacer(1, 10))

    # Hash display box
    hash_data = [[f"SHA-256: {file_hash}"]]
    hash_table = Table(hash_data, colWidths=[6.5 * inch])
    hash_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
        ("FONTNAME", (0, 0), (-1, -1), "Courier"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 1, BORDER_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(hash_table)

    # Footer
    story.append(Spacer(1, 30))
    story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))
    story.append(Paragraph(
        "This certificate was automatically generated by the Sanctions Screening Service. "
        "Screening performed against UN Security Council, SEMA (Canada), and Public Safety Canada sanctions lists. "
        f"Report generated on {timestamp}.",
        styles["SmallText"]
    ))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        "This document is confidential and intended solely for compliance record-keeping purposes.",
        styles["SmallText"]
    ))

    # Build PDF
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def _get_source_abbrev(source: str) -> str:
    """Get abbreviated source name for table display."""
    if "UN" in source:
        return "UN"
    if "SEMA" in source:
        return "SEMA"
    if "Public Safety" in source or "PSC" in source:
        return "PSC"
    return source[:10]


def generate_certificate_of_search(
    results: dict,
    file_hash: str,
    metadata: Optional[dict] = None,
    organization_name: str = "Sanctions Screening Service",
) -> bytes:
    """
    Generate a Certificate of Search PDF with adjudication notes.

    This is the final certificate generated after all matches have been
    reviewed and adjudicated (confirmed or dismissed).

    Args:
        results: Screening results dict containing flagged_results with adjudication status
        file_hash: SHA-256 hash of the uploaded file
        metadata: Optional dict with original_filename, adjudication_completed, etc.
        organization_name: Name to display in header

    Returns:
        PDF file content as bytes
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )

    # Build styles
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="CertHeader",
        fontSize=20,
        fontName="Helvetica-Bold",
        textColor=NAVY,
        alignment=TA_CENTER,
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="CertSubHeader",
        fontSize=14,
        fontName="Helvetica-Bold",
        textColor=TEXT_DARK,
        alignment=TA_CENTER,
        spaceAfter=20,
    ))
    styles.add(ParagraphStyle(
        name="CertSectionTitle",
        fontSize=12,
        fontName="Helvetica-Bold",
        textColor=NAVY,
        spaceBefore=15,
        spaceAfter=8,
    ))
    styles.add(ParagraphStyle(
        name="CertBodyText",
        fontSize=10,
        fontName="Helvetica",
        textColor=TEXT_DARK,
        leading=14,
    ))
    styles.add(ParagraphStyle(
        name="CertSmallText",
        fontSize=8,
        fontName="Helvetica",
        textColor=TEXT_MUTED,
        leading=10,
    ))
    styles.add(ParagraphStyle(
        name="CertNumber",
        fontSize=10,
        fontName="Helvetica",
        textColor=TEXT_MUTED,
        alignment=TA_RIGHT,
    ))
    styles.add(ParagraphStyle(
        name="CertStatusClear",
        fontSize=18,
        fontName="Helvetica-Bold",
        textColor=SUCCESS_GREEN,
        alignment=TA_CENTER,
        spaceBefore=15,
        spaceAfter=15,
    ))
    styles.add(ParagraphStyle(
        name="CertStatusReview",
        fontSize=18,
        fontName="Helvetica-Bold",
        textColor=WARNING_RED,
        alignment=TA_CENTER,
        spaceBefore=15,
        spaceAfter=15,
    ))

    # Build document content
    story = []
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    certificate_id = f"COS-{file_hash[:12].upper()}"

    # Header / Letterhead
    story.append(Paragraph(organization_name, styles["CertHeader"]))
    story.append(Paragraph("Certificate of Search", styles["CertSubHeader"]))
    story.append(HRFlowable(width="100%", thickness=3, color=NAVY, spaceBefore=0, spaceAfter=20))

    # Certificate metadata - compact format
    story.append(Paragraph(f"Certificate ID: {certificate_id} | Generated: {timestamp}", styles["CertNumber"]))
    story.append(Spacer(1, 10))

    # Extract counts
    total_screened = results.get("total_screened", 0)
    flagged_results = results.get("flagged_results", [])

    # Count adjudication outcomes
    confirmed_matches = [r for r in flagged_results if r.get("status") == "confirmed"]
    dismissed_matches = [r for r in flagged_results if r.get("status") == "dismissed"]

    # Certification Statement
    story.append(Paragraph("Certification Statement", styles["CertSectionTitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))

    if len(confirmed_matches) == 0:
        # All clear or all dismissed
        story.append(Paragraph("SCREENING CLEARED", styles["CertStatusClear"]))
        story.append(Paragraph(
            f"This certifies that {total_screened} name(s) were screened against the "
            "UN Security Council, SEMA (Canada), and Public Safety Canada consolidated sanctions lists. "
            f"After review, {len(dismissed_matches)} potential match(es) were investigated and "
            "determined to be false positives. No confirmed sanctions matches were identified.",
            styles["CertBodyText"]
        ))
    else:
        # Has confirmed matches
        story.append(Paragraph("CONFIRMED MATCHES FOUND", styles["CertStatusReview"]))
        story.append(Paragraph(
            f"This certifies that {total_screened} name(s) were screened against the "
            "UN Security Council, SEMA (Canada), and Public Safety Canada consolidated sanctions lists. "
            f"After review, {len(confirmed_matches)} match(es) were confirmed as potential sanctions hits "
            "requiring further action.",
            styles["CertBodyText"]
        ))

    # Use less spacing for clear screenings to fit on one page
    if len(confirmed_matches) == 0:
        story.append(Spacer(1, 8))
    else:
        story.append(Spacer(1, 15))

    # Summary Statistics Table
    story.append(Paragraph("Screening Summary", styles["CertSectionTitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))

    summary_data = [
        ["Metric", "Count"],
        ["Total Names Screened", str(total_screened)],
        ["Initial Potential Matches", str(len(flagged_results))],
        ["Dismissed (False Positives)", str(len(dismissed_matches))],
        ["Confirmed Matches", str(len(confirmed_matches))],
    ]

    summary_table = Table(summary_data, colWidths=[3.5 * inch, 1.5 * inch])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (-1, -1), LIGHT_GRAY),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(summary_table)

    # Confirmed Matches Detail (if any)
    if confirmed_matches:
        story.append(Spacer(1, 20))
        story.append(Paragraph("Confirmed Matches - Requiring Action", styles["CertSectionTitle"]))
        story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))

        # Create style for table text with wrapping
        table_text_style = ParagraphStyle(
            name="TableText",
            fontSize=8,
            fontName="Helvetica",
            leading=10,
            wordWrap='CJK',
        )

        confirmed_header = ["Client Name", "Matched Name", "Score", "Source", "Status"]
        confirmed_data = [confirmed_header]

        for match in confirmed_matches:
            confirmed_data.append([
                Paragraph(match.get("client_name", "N/A"), table_text_style),
                Paragraph(match.get("matched_name", "N/A"), table_text_style),
                f"{match.get('score', 0)}%",
                _get_source_abbrev(match.get("source_list", "")),
                "CONFIRMED",
            ])

        confirmed_table = Table(
            confirmed_data,
            colWidths=[1.6 * inch, 1.6 * inch, 0.6 * inch, 0.8 * inch, 1 * inch]
        )
        confirmed_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), WARNING_RED),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#fef2f2")),
            ("TEXTCOLOR", (4, 1), (4, -1), WARNING_RED),
            ("FONTNAME", (4, 1), (4, -1), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(confirmed_table)

    # Dismissed Matches Detail (if any)
    if dismissed_matches:
        # Use less spacing for clear screenings to fit on one page
        if len(confirmed_matches) == 0:
            story.append(Spacer(1, 10))
        else:
            story.append(Spacer(1, 20))
        story.append(Paragraph("Dismissed Matches - False Positives", styles["CertSectionTitle"]))
        story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))

        # Create style for table text with wrapping
        table_text_style = ParagraphStyle(
            name="TableText",
            fontSize=8,
            fontName="Helvetica",
            leading=10,
            wordWrap='CJK',
        )

        dismissed_header = ["Client Name", "Matched Name", "Score", "Source", "Status"]
        dismissed_data = [dismissed_header]

        for match in dismissed_matches[:15]:  # Limit for readability
            dismissed_data.append([
                Paragraph(match.get("client_name", "N/A"), table_text_style),
                Paragraph(match.get("matched_name", "N/A"), table_text_style),
                f"{match.get('score', 0)}%",
                _get_source_abbrev(match.get("source_list", "")),
                "DISMISSED",
            ])

        if len(dismissed_matches) > 15:
            dismissed_data.append(["...", f"({len(dismissed_matches) - 15} more)", "", "", ""])

        dismissed_table = Table(
            dismissed_data,
            colWidths=[1.6 * inch, 1.6 * inch, 0.6 * inch, 0.8 * inch, 1 * inch]
        )
        dismissed_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#64748b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 1), (-1, -1), LIGHT_GRAY),
            ("TEXTCOLOR", (4, 1), (4, -1), SUCCESS_GREEN),
            ("FONTNAME", (4, 1), (4, -1), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(dismissed_table)

    # File Metadata Section
    if metadata:
        # Use less spacing for clear screenings to fit on one page
        if len(confirmed_matches) == 0:
            story.append(Spacer(1, 10))
        else:
            story.append(Spacer(1, 20))
        story.append(Paragraph("File Information", styles["CertSectionTitle"]))
        story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))

        # Create style for table text with wrapping
        table_text_style = ParagraphStyle(
            name="TableText",
            fontSize=9,
            fontName="Helvetica",
            leading=11,
            wordWrap='CJK',
        )

        meta_data = [["Field", "Value"]]
        for key, value in metadata.items():
            display_key = key.replace("_", " ").title()
            meta_data.append([display_key, Paragraph(str(value), table_text_style)])

        meta_table = Table(meta_data, colWidths=[2 * inch, 4.5 * inch])
        meta_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 1), (-1, -1), LIGHT_GRAY),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(meta_table)

    # Digital Fingerprint Section
    # Use less spacing for clear screenings to fit on one page
    if len(confirmed_matches) == 0:
        story.append(Spacer(1, 10))
    else:
        story.append(Spacer(1, 25))
    story.append(Paragraph("Digital Fingerprint", styles["CertSectionTitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))

    # Use compact description for clear screenings
    if len(confirmed_matches) == 0:
        story.append(Paragraph(
            "SHA-256 fingerprint linking this certificate to the original uploaded file:",
            styles["CertBodyText"]
        ))
        story.append(Spacer(1, 4))
    else:
        story.append(Paragraph(
            "This certificate is cryptographically linked to the original uploaded file using SHA-256. "
            "This fingerprint verifies the authenticity and integrity of the screening results.",
            styles["CertBodyText"]
        ))
        story.append(Spacer(1, 8))

    # Hash display box
    hash_data = [[f"SHA-256: {file_hash}"]]
    hash_table = Table(hash_data, colWidths=[6.5 * inch])
    hash_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
        ("FONTNAME", (0, 0), (-1, -1), "Courier"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 1, BORDER_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(hash_table)

    # Footer
    # Use less spacing for clear screenings to fit on one page
    if len(confirmed_matches) == 0:
        story.append(Spacer(1, 5))
        story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=6))
        # Compact footer for clear screenings
        story.append(Paragraph(
            "This Certificate of Search was automatically generated by the Sanctions Screening Service. "
            "Screening performed against UN Security Council, SEMA (Canada), and Public Safety Canada sanctions lists. "
            "This document is confidential and intended solely for compliance record-keeping purposes.",
            styles["CertSmallText"]
        ))
    else:
        story.append(Spacer(1, 25))
        story.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceBefore=0, spaceAfter=10))
        story.append(Paragraph(
            "This Certificate of Search was automatically generated by the Sanctions Screening Service. "
            "Screening performed against UN Security Council, SEMA (Canada), and Public Safety Canada sanctions lists. "
            f"Certificate generated on {timestamp}.",
            styles["CertSmallText"]
        ))
        story.append(Spacer(1, 5))
        story.append(Paragraph(
            "This document is confidential and intended solely for compliance record-keeping purposes. "
            "Retain this certificate for your records as proof of sanctions screening compliance.",
            styles["CertSmallText"]
        ))

    # Build PDF
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
