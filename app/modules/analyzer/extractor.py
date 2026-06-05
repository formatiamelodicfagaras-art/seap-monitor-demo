import os
import zipfile
import tempfile
from pathlib import Path

from loguru import logger


def extract_text_from_pdf(file_path: str) -> str:
    """Extract text content from a PDF file using pdfplumber."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n\n".join(text_parts)
    except Exception as e:
        logger.error(f"Error extracting text from PDF {file_path}: {e}")
        return f"[Eroare extragere PDF: {e}]"


def extract_text_from_docx(file_path: str) -> str:
    """Extract text content from a DOCX file using python-docx."""
    try:
        from docx import Document
        doc = Document(file_path)
        text_parts = []
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                text_parts.append(paragraph.text)

        # Also extract text from tables
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    text_parts.append(row_text)

        return "\n".join(text_parts)
    except Exception as e:
        logger.error(f"Error extracting text from DOCX {file_path}: {e}")
        return f"[Eroare extragere DOCX: {e}]"


def extract_text_from_zip(file_path: str) -> str:
    """Extract and process files from a ZIP archive."""
    try:
        text_parts = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(file_path, "r") as zf:
                zf.extractall(tmp_dir)

            # Process extracted files
            for root, _, files in os.walk(tmp_dir):
                for fname in sorted(files):
                    fpath = os.path.join(root, fname)
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

                    if ext == "pdf":
                        text = extract_text_from_pdf(fpath)
                        if text:
                            text_parts.append(f"--- {fname} ---\n{text}")
                    elif ext == "docx":
                        text = extract_text_from_docx(fpath)
                        if text:
                            text_parts.append(f"--- {fname} ---\n{text}")

        return "\n\n".join(text_parts)
    except Exception as e:
        logger.error(f"Error extracting text from ZIP {file_path}: {e}")
        return f"[Eroare extragere ZIP: {e}]"


def extract_text(file_path: str, file_type: str | None = None) -> str:
    """Extract text from a file based on its type."""
    if not os.path.exists(file_path):
        return f"[Fișier inexistent: {file_path}]"

    if file_type is None:
        file_type = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""

    extractors = {
        "pdf": extract_text_from_pdf,
        "docx": extract_text_from_docx,
        "zip": extract_text_from_zip,
    }

    extractor = extractors.get(file_type)
    if extractor:
        return extractor(file_path)

    return f"[Tip fișier nesuportat: {file_type}]"
