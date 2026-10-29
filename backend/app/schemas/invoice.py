"""Typed invoice extraction, evidence, and validation contracts.

Money is represented by Decimal internally and decimal strings in JSON.
Evidence references existing ReadingUnit.sequence values.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    field_validator,
)

from app.services.reading_types import DocumentSource, SourcePosition


HEADER_FIELDS = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "currency",
    "subtotal",
    "tax",
    "total",
)

LINE_FIELDS = (
    "description",
    "quantity",
    "unit_price",
    "line_total",
)

FIELD_PATH_PATTERN = (
    r"^(invoice_number|invoice_date|supplier_name|customer_name|"
    r"currency|subtotal|tax|total|"
    r"line_items\.[0-9]+\.(description|quantity|unit_price|line_total))$"
)

DECIMAL_PATTERN = r"^-?[0-9]{1,16}(\.[0-9]{1,6})?$"


def parse_decimal(value: object) -> Decimal:
    """Reject binary floats, booleans, non-finite and excessive values."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("Use a decimal string, Decimal, or integer.")

    if not isinstance(value, (str, Decimal, int)):
        raise ValueError("Invalid decimal value.")

    text = str(value).strip()
    if not re.fullmatch(DECIMAL_PATTERN, text):
        raise ValueError(
            "Expected a plain decimal with at most 16 integer digits "
            "and 6 fractional digits."
        )

    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal value.") from exc

    if not result.is_finite():
        raise ValueError("Decimal must be finite.")

    return result


Amount = Annotated[
    Decimal,
    BeforeValidator(parse_decimal),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": DECIMAL_PATTERN,
            "description": (
                "Exact decimal string without currency symbols, commas, "
                "scientific notation, or inferred values."
            ),
        },
        mode="validation",
    ),
]

ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]

FieldPath = Annotated[
    str,
    StringConstraints(pattern=FIELD_PATH_PATTERN, max_length=100),
]


class InvoiceBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceEvidence(InvoiceBase):
    field: FieldPath
    sequence: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=1000)

    @field_validator("quote")
    @classmethod
    def require_nonblank_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Evidence quote cannot be blank.")
        return value


class LineItem(InvoiceBase):
    description: ShortText | None = None
    quantity: Amount | None = None
    unit_price: Amount | None = None
    line_total: Amount | None = None


class Invoice(InvoiceBase):
    invoice_number: ShortText | None = None
    invoice_date: date | None = None
    supplier_name: ShortText | None = None
    customer_name: ShortText | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    subtotal: Amount | None = None
    tax: Amount | None = None
    total: Amount | None = None
    line_items: list[LineItem] = Field(default_factory=list, max_length=5000)
    line_items_complete: bool = False
    uncertain_fields: list[FieldPath] = Field(
        default_factory=list,
        max_length=20000,
    )
    evidence: list[SourceEvidence] = Field(
        default_factory=list,
        max_length=30000,
    )


class ValidationIssue(InvoiceBase):
    code: str
    severity: Literal["warning", "error"]
    field: str
    message: str
    expected: str | None = None
    actual: str | None = None
    related_document_id: UUID | None = None


class VerifiedEvidence(SourceEvidence):
    position: SourcePosition


class InvoiceResult(InvoiceBase):
    schema_version: Literal["1"] = "1"
    source: DocumentSource
    provider: Literal["gemini", "test"]
    model: str
    invoice: Invoice
    issues: list[ValidationIssue]
    missing_fields: list[str]
    review_required: bool
    confidence: Literal["source_supported", "review_required"]
    evidence: list[VerifiedEvidence]
    input_units: int = Field(ge=1)
    chunks_processed: int = Field(ge=1)
