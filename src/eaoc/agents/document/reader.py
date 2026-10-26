"""
EAOC Document Agent - Document Reader Interface & Dependency Boundary
Defines abstract BaseDocumentReader and InMemoryDocumentReader to decouple
document retrieval from concrete storage systems (e.g. S3, Supabase Storage, local disk).
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple


class BaseDocumentReader(ABC):
    """
    Dependency boundary / abstraction for retrieving document contents by document_id.
    """

    @abstractmethod
    async def read_document(self, document_id: str) -> Tuple[bytes, str]:
        """
        Retrieves document content bytes and format extension.
        Returns:
            Tuple[bytes, str]: (raw_content_bytes, format_extension)
        Raises:
            FileNotFoundError: If the document does not exist.
        """
        pass


class InMemoryDocumentReader(BaseDocumentReader):
    """
    In-memory document reader stub for deterministic testing and local verification.
    """

    def __init__(self, documents: Optional[Dict[str, Tuple[bytes, str]]] = None):
        # Mapping: document_id -> (content_bytes, format)
        self._documents: Dict[str, Tuple[bytes, str]] = documents or {}

    def add_document(self, document_id: str, content: bytes, doc_format: str) -> None:
        self._documents[document_id] = (content, doc_format.lower().strip().lstrip("."))

    async def read_document(self, document_id: str) -> Tuple[bytes, str]:
        if document_id not in self._documents:
            raise FileNotFoundError(f"Document with ID '{document_id}' not found.")
        return self._documents[document_id]
