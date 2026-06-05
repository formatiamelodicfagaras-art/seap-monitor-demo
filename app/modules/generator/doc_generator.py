import json
from datetime import datetime
from pathlib import Path

import anthropic
from loguru import logger

from app.config import get_settings
from app.services.file_service import get_generated_path

settings = get_settings()

GENERATOR_SYSTEM_PROMPT = """
Esti un expert in pregatirea ofertelor pentru licitatii publice SEAP Romania.

Ai la dispozitie:
1. TEMPLATE — un document model dintr-o licitatie castigata anterior
2. ANALIZA — cerintele extrase din caietul de sarcini al licitatiei curente
3. DATE FIRMA — informatiile fixe ale ofertantului
4. DATE LICITATIE — nr anunt, autoritate, termen, valoare, etc.

Sarcina ta:
- Genereaza continutul documentului adaptat la licitatia curenta
- Pastreaza EXACT structura si formatarea template-ului
- Completeaza TOATE campurile variabile cu datele disponibile:
  - Nr. anunt/procedura: din DATE LICITATIE (notice_no)
  - Autoritate contractanta: din DATE LICITATIE (contracting_authority)
  - Data limita depunere: din DATE LICITATIE (deadline)
  - Obiect contract: din DATE LICITATIE (title)
  - Cod CPV: din DATE LICITATIE (cpv_code)
  - Valoare estimata: din DATE LICITATIE (estimated_value)
  - Cerinte tehnice: din ANALIZA
- OBLIGATORIU: In fiecare document, datele firmei ofertante trebuie sa apara COMPLET:
  - Denumire firma completa (name)
  - CUI (cui)
  - Nr. Registrul Comertului (reg_com)
  - Adresa completa (address)
  - Banca si IBAN (bank_name, iban)
  - Reprezentant legal, nume si functie (legal_rep, legal_rep_role)
  - Telefon (phone)
  - Email (email)
  NU omite niciun camp! Toate datele din DATE FIRMA trebuie incluse in document.
- NU lasa campuri necompletate daca datele sunt disponibile
- Doar daca informatia LIPSESTE COMPLET, marcheaza cu [DE COMPLETAT: descriere]
- La sfarsitul documentului, unde trebuie semnatura reprezentantului legal, pune EXACT textul: [SEMNATURA SI STAMPILA]
- Pune [SEMNATURA SI STAMPILA] pe un rand separat, dupa numele si functia reprezentantului

IMPORTANT despre formatare:
- NU repeta NICIODATA titlul documentului. Titlul apare EXACT O SINGURA DATA la inceputul documentului.
- Daca template-ul are titlul repetat, pune-l O SINGURA DATA.
- NU pune acelasi text ca heading SI ca paragraf.
- Returneaza text simplu, curat, fara marcaje === sau ---
- Foloseste **text bold** pentru TOATE etichetele/label-urile din document: **Către:**, **Adresa:**, **Nr. anunț:**, **Data limită:**, **Subsemnatul**, **Numele și prenumele semnatarului:**, **Funcția:**, **Data:**, **Semnătura ofertantului**, etc.
- Scrie INTOTDEAUNA cu diacritice romanesti corecte: ă, â, î, ș, ț (NU a, i, s, t)
- Exemple corecte: către, adresă, ofertă, licitație, achizitie, contractantă, funcția, ștampilă
- Formularea de adresare este INTOTDEAUNA: "Stimată doamnă / Stimate domn," sau "Stimați domni," (NU "Domnilor,")
"""


async def generate_document(
    template_text: str,
    analysis_data: dict,
    company_data: dict,
    notice_data: dict,
) -> str:
    """Generate a bid document using AI."""
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_message = f"""
=== DATE LICITATIE ===
{json.dumps(notice_data, ensure_ascii=False, indent=2)}

=== TEMPLATE (document model) ===
{template_text[:80000]}

=== ANALIZA CAIET DE SARCINI ===
{json.dumps(analysis_data, ensure_ascii=False, indent=2)[:30000]}

=== DATE FIRMA OFERTANTA ===
{json.dumps(company_data, ensure_ascii=False, indent=2)}

Completeaza TOATE campurile cu datele disponibile. Nr anunt = {notice_data.get('notice_no', '')}. Termen limita = {notice_data.get('deadline', '')}. Autoritate = {notice_data.get('contracting_authority', '')}.
"""

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=GENERATOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        return response.content[0].text.strip()

    except Exception as e:
        logger.error(f"Document generation failed: {e}")
        raise


def save_as_docx(content: str, output_path: str, signature_path: str = "", stamp_path: str = ""):
    """Save generated content as a DOCX file with optional signature and stamp."""
    from docx import Document
    from docx.shared import Pt, Inches, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    import os

    doc = Document()

    # Set generic document properties; runtime callers can override afterward.
    doc.core_properties.author = "SEAP Monitor"
    doc.core_properties.company = ""
    doc.core_properties.last_modified_by = "SEAP Monitor"

    # Set default font
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Arial"
    font.size = Pt(11)

    signature_inserted = False
    lines = content.split("\n")
    # Remove excessive empty lines (max 1 consecutive) and duplicate lines
    cleaned_lines = []
    empty_count = 0
    seen_headings = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            empty_count += 1
            if empty_count <= 1:
                cleaned_lines.append(line)
        else:
            empty_count = 0
            # De-duplicate: skip if same text as previous non-empty line (case insensitive)
            norm = stripped.lower().strip("*= -")
            if norm and len(norm) > 5:
                if norm in seen_headings:
                    continue  # skip duplicate
                # Track headings/titles (all-caps or bold lines)
                if stripped.isupper() or (stripped.startswith("**") and stripped.endswith("**")):
                    seen_headings.add(norm)
            cleaned_lines.append(line)

    # Pre-process: collect pipe table blocks and convert inline
    i = 0
    final_lines = []
    while i < len(cleaned_lines):
        line = cleaned_lines[i].strip()
        # Detect pipe table row (at least 2 pipes, not a separator line)
        if "|" in line and line.count("|") >= 2 and not all(c in "|-: " for c in line):
            # Collect consecutive pipe rows
            table_rows = []
            while i < len(cleaned_lines):
                l = cleaned_lines[i].strip()
                if "|" in l and l.count("|") >= 2:
                    if not all(c in "|-: " for c in l):
                        cells = [c.strip() for c in l.strip("|").split("|")]
                        table_rows.append(cells)
                    i += 1
                elif not l:
                    i += 1
                    if i < len(cleaned_lines) and "|" in cleaned_lines[i].strip():
                        continue
                    break
                else:
                    break
            if table_rows:
                final_lines.append(("TABLE", table_rows))
            continue
        final_lines.append(("TEXT", line))
        i += 1

    for item_type, item_data in final_lines:
        if item_type == "TABLE":
            from docx.shared import Pt as TblPt, RGBColor as TblRGB
            table_rows = item_data
            max_cols = max(len(row) for row in table_rows)
            tbl = doc.add_table(rows=len(table_rows), cols=max_cols)
            tbl.style = "Table Grid"
            for row_idx, row_data in enumerate(table_rows):
                for col_idx, cell_text in enumerate(row_data):
                    if col_idx < max_cols:
                        cell = tbl.cell(row_idx, col_idx)
                        cell.text = ""
                        paragraph = cell.paragraphs[0]
                        # Check for [DE COMPLETAT] in cell text
                        if "[DE COMPLETAT" in cell_text.upper():
                            run = paragraph.add_run(cell_text)
                            run.font.size = TblPt(9)
                            run.bold = True
                            run.font.color.rgb = TblRGB(0xCC, 0x00, 0x00)
                            run.font.highlight_color = 7  # Yellow
                        else:
                            run = paragraph.add_run(cell_text)
                            run.font.size = TblPt(9)
                            if row_idx == 0:
                                run.bold = True
            continue

        line = item_data
        if not line:
            doc.add_paragraph("")
            continue

        # Handle section headers
        if line.startswith("=== ") and line.endswith(" ==="):
            heading_text = line.strip("= ")
            doc.add_heading(heading_text, level=1)
        elif line.startswith("--- ") and line.endswith(" ---"):
            heading_text = line.strip("- ")
            doc.add_heading(heading_text, level=2)
        elif "[SEMNATURA]" in line.upper() or "[STAMPILA]" in line.upper() or "[SEMNATURA SI STAMPILA]" in line.upper():
            # Insert signature and stamp side by side using a borderless table
            _insert_signature_stamp(doc, signature_path, stamp_path, Cm)
            signature_inserted = True
        else:
            _add_rich_paragraph(doc, line)


    # If signature wasn't inserted via marker, add at the end
    if not signature_inserted and (signature_path or stamp_path):
        _insert_signature_stamp(doc, signature_path, stamp_path, Cm)

    doc.save(output_path)
    logger.info(f"DOCX saved: {output_path}")


def _insert_signature_stamp(doc, signature_path, stamp_path, Cm):
    """Insert signature and stamp — signature on left overlapping stamp on right."""
    import os
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(0)

    # Signature first (left), then stamp next to it (right)
    # They appear inline next to each other, signature slightly overlapping stamp
    if signature_path and os.path.exists(signature_path):
        run = p.add_run()
        run.add_picture(signature_path, height=Cm(2))
    if stamp_path and os.path.exists(stamp_path):
        # Small negative indent to overlap
        run = p.add_run()
        run.add_picture(stamp_path, height=Cm(2.5))


def _add_rich_paragraph(doc, line):
    """Add a paragraph with inline **bold**, numbered bold, and [DE COMPLETAT] in red+yellow."""
    import re
    from docx.shared import RGBColor

    p = doc.add_paragraph()

    # Check if line starts with a number like "1." or "2." — bold the number
    num_match = re.match(r'^(\d+\.\s*)', line)
    if num_match:
        run = p.add_run(num_match.group(1))
        run.bold = True
        line = line[num_match.end():]

    # Split by bold markers and [DE COMPLETAT: ...] markers
    pattern = r'(\*\*.*?\*\*|\[DE COMPLETAT[^\]]*\])'
    parts = re.split(pattern, line)

    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = p.add_run(part[2:-2])
            run.bold = True
        elif part.upper().startswith("[DE COMPLETAT"):
            run = p.add_run(part)
            run.bold = True
            run.font.color.rgb = RGBColor(0xCC, 0x00, 0x00)
            run.font.highlight_color = 7  # Yellow highlight
        else:
            p.add_run(part)


def _convert_pipe_tables(doc):
    """Find consecutive paragraphs with | delimiters and convert to real Word tables."""
    from docx.shared import Pt, Cm
    from docx.oxml.ns import qn

    paragraphs = list(doc.paragraphs)
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        text = p.text.strip()

        # Check if this line looks like a pipe-delimited table row
        if "|" in text and text.count("|") >= 2:
            # Collect consecutive pipe-delimited lines
            table_rows = []
            j = i
            while j < len(paragraphs):
                t = paragraphs[j].text.strip()
                if "|" in t and t.count("|") >= 2:
                    # Skip separator lines like |---|---|
                    if not all(c in "|-: " for c in t):
                        cells = [c.strip() for c in t.strip("|").split("|")]
                        table_rows.append(cells)
                    j += 1
                elif not t:
                    j += 1  # skip empty lines between table rows
                    # But only if next line is also a table row
                    if j < len(paragraphs) and "|" in paragraphs[j].text:
                        continue
                    else:
                        break
                else:
                    break

            if len(table_rows) >= 2:
                # Determine max columns
                max_cols = max(len(row) for row in table_rows)

                # Insert table before first paragraph
                tbl = doc.add_table(rows=len(table_rows), cols=max_cols)
                tbl.style = "Table Grid"

                for row_idx, row_data in enumerate(table_rows):
                    for col_idx, cell_text in enumerate(row_data):
                        if col_idx < max_cols:
                            cell = tbl.cell(row_idx, col_idx)
                            cell.text = cell_text
                            # Bold header row
                            if row_idx == 0:
                                for paragraph in cell.paragraphs:
                                    for run in paragraph.runs:
                                        run.bold = True
                                        run.font.size = Pt(9)
                            else:
                                for paragraph in cell.paragraphs:
                                    for run in paragraph.runs:
                                        run.font.size = Pt(9)

                # Move table to position of first pipe paragraph
                p._element.addnext(tbl._tbl)

                # Remove original pipe paragraphs
                for k in range(i, j):
                    if k < len(paragraphs):
                        parent = paragraphs[k]._element.getparent()
                        if parent is not None:
                            parent.remove(paragraphs[k]._element)

                # Don't increment i since we removed paragraphs
                continue

        i += 1


def save_as_pdf(content: str, output_path: str):
    """Save generated content as a PDF file."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.units import cm

    doc = SimpleDocTemplate(output_path, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 0.3 * cm))
            continue

        if line.startswith("=== ") and line.endswith(" ==="):
            heading_text = line.strip("= ")
            story.append(Paragraph(heading_text, styles["Heading1"]))
        elif line.startswith("--- ") and line.endswith(" ---"):
            heading_text = line.strip("- ")
            story.append(Paragraph(heading_text, styles["Heading2"]))
        else:
            story.append(Paragraph(line, styles["Normal"]))

    doc.build(story)
    logger.info(f"PDF saved: {output_path}")
