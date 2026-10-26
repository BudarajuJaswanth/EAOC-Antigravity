"""
EAOC Analytics Agent - Data Provider Interface & Dependency Boundary
Defines abstract BaseDataProvider and InMemoryDataProvider to decouple
structured dataset resolution from direct database access.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseDataProvider(ABC):
    """
    Dependency boundary / abstraction for retrieving structured datasets for analysis.
    Prevents AnalyticsAgent from querying production databases directly.
    """

    @abstractmethod
    async def get_data(
        self, data_source: str, query_spec: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieves a tabular dataset as a list of dictionary row records.
        Raises:
            LookupError: If data_source cannot be resolved.
        """
        pass


class InMemoryDataProvider(BaseDataProvider):
    """
    In-memory data provider stub for deterministic testing and local verification.
    """

    def __init__(self, datasets: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._datasets: Dict[str, List[Dict[str, Any]]] = (
            {k.lower(): v for k, v in datasets.items()} if datasets else {}
        )

    def set_dataset(self, name: str, rows: List[Dict[str, Any]]) -> None:
        self._datasets[name.lower()] = rows

    async def get_data(
        self, data_source: str, query_spec: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        target = data_source.lower().strip()
        if target not in self._datasets:
            raise LookupError(f"Data source '{data_source}' not found in data provider.")
        return list(self._datasets[target])
