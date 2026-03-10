"""
Resume PDF exporter — matches Harshit Bedi resume format exactly.

Layout:
  - Centered bold name (large)
  - Centered contact line with | separators
  - Section headers: ALL CAPS, bold, full-width rule below
  - Skills: Bold Category: skills text
  - Experience/Education: Bold Role  |  Date (right-aligned), Italic Company below
  - Bullet points with • character
  - Projects: Bold Name | (tech) | Year (right-aligned)

Usage:
    from resume.pdf_export import render_pdf
    path = render_pdf(markdown_text, output_path="resume/output/harshit_tailored.pdf")
"""

import re
from pathlib import Path
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ── Page geometry ──────────────────────────────────────────────────────────
LEFT_MARGIN = 0.55 * inch
RIGHT_MARGIN = 0.55 * inch
TOP_MARGIN = 0.5 * inch
BOTTOM_MARGIN = 0.5 * inch

# ── Colours ────────────────────────────────────────────────────────────────
BLACK = colors.black
RULE_COLOR = colors.black

# ── Font sizes ─────────────────────────────────────────────────────────────
NAME_SIZE = 22
CONTACT_SIZE = 10
SECTION_SIZE = 11
BODY_SIZE = 10
SMALL_SIZE = 9.5


def _styles():
    """Return a dict of all named ParagraphStyles."""
    base = dict(
        fontName="Helvetica",
        fontSize=BODY_SIZE,
        leading=14,
        textColor=BLACK,
    )

    return {
        "name": ParagraphStyle(
            "name", **{**base,
            "fontName": "Helvetica-Bold",
            "fontSize": NAME_SIZE,
            "leading": NAME_SIZE + 4,
            "alignment": TA_CENTER,
            "spaceAfter": 2,
        }),
        "contact": ParagraphStyle(
            "contact", **{**base,
            "fontSize": CONTACT_SIZE,
            "leading": 13,
            "alignment": TA_CENTER,
            "spaceAfter": 8,
        }),
        "section": ParagraphStyle(
            "section", **{**base,
            "fontName": "Helvetica-Bold",
            "fontSize": SECTION_SIZE,
            "leading": SECTION_SIZE + 4,
            "spaceBefore": 10,
            "spaceAfter": 2,
        }),
        "role_left": ParagraphStyle(
            "role_left", **{**base,
            "fontName": "Helvetica-Bold",
            "fontSize": BODY_SIZE,
            "leading": 13,
        }),
        "date_right": ParagraphStyle(
            "date_right", **{**base,
            "fontSize": BODY_SIZE,
            "leading": 13,
            "alignment": TA_RIGHT,
        }),
        "company": ParagraphStyle(
            "company", **{**base,
            "fontName": "Helvetica-Oblique",
            "fontSize": BODY_SIZE,
            "leading": 13,
            "spaceAfter": 2,
        }),
        "bullet": ParagraphStyle(
            "bullet", **{**base,
            "fontSize": BODY_SIZE,
            "leading": 13,
            "leftIndent": 12,
            "firstLineIndent": -12,
            "spaceAfter": 1,
        }),
        "body": ParagraphStyle(
            "body", **{**base,
            "fontSize": BODY_SIZE,
            "leading": 13,
            "spaceAfter": 2,
        }),
        "summary": ParagraphStyle(
            "summary", **{**base,
            "fontSize": BODY_SIZE,
            "leading": 13,
            "spaceAfter": 4,
        }),
    }


def _rule():
    return HRFlowable(
        width="100%",
        thickness=0.8,
        color=RULE_COLOR,
        spaceAfter=4,
        spaceBefore=0,
    )


def _bold(text: str) -> str:
    """Wrap text in reportlab bold tags."""
    return f"<b>{text}</b>"


def _italic(text: str) -> str:
    return f"<i>{text}</i>"


def _escape(text: str) -> str:
    """Escape XML special chars for reportlab Paragraph."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _role_date_row(role: str, date: str, styles: dict, page_width: float) -> Table:
    """Create a two-column table: bold role left, date right."""
    col_w = page_width - LEFT_MARGIN - RIGHT_MARGIN
    t = Table(
        [[Paragraph(_bold(_escape(role)), styles["role_left"]),
          Paragraph(_escape(date), styles["date_right"])]],
        colWidths=[col_w * 0.70, col_w * 0.30],
        hAlign="LEFT",
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _apply_inline(text: str) -> str:
    """Convert **bold** and *italic* markdown to reportlab XML tags."""
    # Bold before italic (order matters)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    return text


def _parse_markdown(md: str, styles: dict, page_width: float) -> list:
    """Parse the resume markdown into a list of reportlab Flowables."""
    flowables = []
    lines = md.strip().splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        # ── Name (# Heading) ───────────────────────────────────────────────
        if line.startswith("# ") and i == 0:
            name = line[2:].strip()
            flowables.append(Paragraph(_escape(name), styles["name"]))
            i += 1
            continue

        # ── Contact line (line after name, before any ##) ─────────────────
        if i == 1 and not line.startswith("#"):
            flowables.append(Paragraph(_escape(line), styles["contact"]))
            i += 1
            continue

        # ── Section headers (## HEADING) ──────────────────────────────────
        if line.startswith("## "):
            heading = line[3:].strip().upper()
            flowables.append(Paragraph(_escape(heading), styles["section"]))
            flowables.append(_rule())
            i += 1
            continue

        # ── Role | Date lines (bold role + pipe + date) ───────────────────
        # Pattern: **Role Title** | Date Range
        role_date_match = re.match(r"\*\*(.+?)\*\*\s*\|\s*(.+)", line)
        if role_date_match:
            role = role_date_match.group(1).strip()
            date = role_date_match.group(2).strip()
            flowables.append(_role_date_row(role, date, styles, page_width))
            i += 1
            # Next line: *Company | Location*
            if i < len(lines) and lines[i].startswith("*") and lines[i].endswith("*"):
                company = lines[i].strip("*").strip()
                flowables.append(Paragraph(_escape(company), styles["company"]))
                i += 1
            continue

        # ── Skill lines (**Category:** skills) ────────────────────────────
        skill_match = re.match(r"\*\*(.+?):\*\*\s*(.*)", line)
        if skill_match:
            cat = skill_match.group(1)
            skills_text = skill_match.group(2)
            p_text = f"<b>{_escape(cat)}:</b> {_escape(skills_text)}"
            flowables.append(Paragraph(p_text, styles["body"]))
            i += 1
            continue

        # ── Bullet points ─────────────────────────────────────────────────
        if line.startswith("- "):
            text = line[2:].strip()
            text = _apply_inline(_escape(text))
            flowables.append(Paragraph(f"&#x25CF;  {text}", styles["bullet"]))
            i += 1
            continue

        # ── Empty line ────────────────────────────────────────────────────
        if not line:
            flowables.append(Spacer(1, 3))
            i += 1
            continue

        # ── Fallback: regular paragraph ───────────────────────────────────
        text = _apply_inline(_escape(line))
        flowables.append(Paragraph(text, styles["summary"]))
        i += 1

    return flowables


def render_pdf(markdown_text: str, output_path: str) -> str:
    """Render a markdown resume string to a PDF file.

    Args:
        markdown_text: The full resume in the HireLoop markdown format.
        output_path: Destination file path for the PDF.

    Returns:
        The absolute path to the generated PDF.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=LETTER,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
    )

    s = _styles()
    flowables = _parse_markdown(markdown_text, s, LETTER[0])
    doc.build(flowables)

    return str(Path(output_path).resolve())


# ── CLI test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    template = Path("resume/variants/harshit_base.md")
    if not template.exists():
        print("No base template found at resume/variants/harshit_base.md")
        sys.exit(1)

    md = template.read_text(encoding="utf-8")
    out = render_pdf(md, "resume/output/test_render.pdf")
    print(f"PDF rendered: {out}")
