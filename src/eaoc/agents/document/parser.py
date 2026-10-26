"""
EAOC Document Agent - Document Parser Implementation
Handles parsing of TXT, PDF (via pypdf), and DOCX (via python-docx) documents into
normalized plain text, page counts, and metadata.
"""

import io
from typing import Any, Dict, NamedTuple


class ParsedDocument(NamedTuple):
    """Container holding normalized document extraction results."""
    text: str
    page_count: int
    metadata: Dict[str, Any]


class DocumentParser:
    """
    Parser for enterprise document formats: PDF, DOCX, and TXT.
    Enforces non-empty output and explicit format validation.
    """

    @staticmethod
    def parse(content: bytes, doc_format: str) -> ParsedDocument:
        """
        Parses document bytes into plain text and page metadata.
        Raises:
            ValueError: If the format is unsupported, malformed, or text is empty.
        """
        fmt = doc_format.lower().strip().lstrip(".")

        if not content:
            raise ValueError("Document content is empty (0 bytes).")

        if fmt == "txt":
            return DocumentParser._parse_txt(content)
        elif fmt == "pdf":
            return DocumentParser._parse_pdf(content)
        elif fmt == "docx":
            return DocumentParser._parse_docx(content)
        else:
            raise ValueError(
                f"Unsupported document format '{fmt}'. "
                "Supported formats: ['pdf', 'docx', 'txt']."
            )

    @staticmethod
    def _parse_txt(content: bytes) -> ParsedDocument:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("latin-1")
            except Exception as exc:
                raise ValueError(f"Failed to decode text document: {exc}")

        stripped = text.strip()
        if not stripped:
            raise ValueError("Parsed document content is empty or unreadable.")

        return ParsedDocument(
            text=stripped,
            page_count=1,
            metadata={"format": "txt", "char_count": len(stripped)},
        )

    @staticmethod
    def _parse_pdf(content: bytes) -> ParsedDocument:
        try:
            import pypdf
        except ImportError:
            raise RuntimeError(
                "pypdf is required to parse PDF documents. Install via pip install pypdf."
            )

        try:
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages_text = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                pages_text.append(page_text)
            
            full_text = "\n\n".join(p.strip() for p in pages_text if p.strip()).strip()
            page_count = len(reader.pages)
        except Exception as exc:
            raise ValueError(f"Failed to parse PDF document: {exc}")

        if not full_text:
            raise ValueError("Parsed PDF document contains no readable text.")

        return ParsedDocument(
            text=full_text,
            page_count=max(1, page_count),
            metadata={"format": "pdf", "page_count": page_count},
        )

    @staticmethod
    def _parse_docx(content: bytes) -> ParsedDocument:
        try:
            import docx
        except ImportError:
            raise RuntimeError(
                "python-docx is required to parse DOCX documents. Install via pip install python-docx."
            )

        try:
            doc = docx.Document(io.BytesIO(content))
            paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            
            # Also extract text from tables if present
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                    if row_text:
                        paragraphs.append(row_text)

            full_text = "\n".join(paragraphs).strip()
        except Exception as exc:
            raise ValueError(f"Failed to parse DOCX document: {exc}")

        if not full_text:
            raise ValueError("Parsed DOCX document contains no readable text.")

        return ParsedDocument(
            text=full_text,
            page_count=1,
            metadata={"format": "docx", "paragraph_count": len(paragraphs)},
        )
