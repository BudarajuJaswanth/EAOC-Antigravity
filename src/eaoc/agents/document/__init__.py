"""
EAOC Package Module: document
Provides the Document specialized agent, parser, reader boundary, and typed schemas.
"""

from eaoc.agents.document.agent import DocumentAgent
from eaoc.agents.document.parser import DocumentParser, ParsedDocument
from eaoc.agents.document.reader import BaseDocumentReader, InMemoryDocumentReader
from eaoc.agents.document.schemas import (
    DocumentFormat,
    DocumentInput,
    DocumentOperation,
    DocumentOutput,
)

__all__ = [
    "DocumentAgent",
    "BaseDocumentReader",
    "InMemoryDocumentReader",
    "DocumentParser",
    "ParsedDocument",
    "DocumentFormat",
    "DocumentOperation",
    "DocumentInput",
    "DocumentOutput",
]
