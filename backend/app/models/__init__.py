"""SQLAlchemy persistence models.

Importing the package registers extraction tables with shared metadata.
Existing ingestion imports continue to register document and job tables.
"""

from app.models.extraction import (
    DocumentExtraction,
    DocumentExtractionHead,
    ExtractedUnit,
)

__all__ = [
    "DocumentExtraction",
    "DocumentExtractionHead",
    "ExtractedUnit",
]
