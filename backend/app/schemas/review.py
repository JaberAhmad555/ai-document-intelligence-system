"""Strict review commands. Money crosses the API as decimal strings only."""
from datetime import date, datetime
import re
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator

from app.schemas.invoice import Amount, LineItem, ShortText, ValidationIssue


def decimal_string(value: object) -> object:
    if value is not None and not isinstance(value, str):
        raise ValueError("Use a decimal string or null, never a JSON number.")
    return value


class ReviewedLine(LineItem):
    @field_validator("quantity", "unit_price", "line_total", mode="before")
    @classmethod
    def strings_only(cls, value: object) -> object:
        return decimal_string(value)


class InvoicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: ShortText | None = None
    invoice_date: date | None = None
    supplier_name: ShortText | None = None
    customer_name: ShortText | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    subtotal: Amount | None = None
    tax: Amount | None = None
    total: Amount | None = None
    line_items: list[ReviewedLine] | None = Field(default=None, max_length=5000)
    line_items_complete: StrictBool | None = None

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def strings_only(cls, value: object) -> object:
        return decimal_string(value)

    @field_validator("invoice_date", mode="before")
    @classmethod
    def iso_date_only(cls, value: object) -> object:
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
        ):
            raise ValueError("Use a YYYY-MM-DD date or null.")
        return value

    @model_validator(mode="after")
    def explicit_changes(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one changed field.")
        for name in ("line_items", "line_items_complete"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null.")
        if "line_items" in self.model_fields_set and "line_items_complete" not in self.model_fields_set:
            raise ValueError("State line_items_complete when replacing line items.")
        return self


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    extraction_id: UUID
    expected_revision: int = Field(strict=True, ge=0, le=2147483646)
    reviewer_label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    changes: InvoicePatch


class CorrectionReceipt(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    request_id: UUID
    document_id: UUID
    extraction_id: UUID
    revision: int
    created_at: datetime


class CorrectionSummary(CorrectionReceipt):
    reviewer_label: str
    reason: str
    changed_fields: list[str]
    original_digest: str
    tolerance: str


class CorrectionDetail(CorrectionSummary):
    corrected_fields: list[str]
    before_values: dict[str, Any]
    after_values: dict[str, Any]
    issues_at_save: list[ValidationIssue]


class CorrectionPage(BaseModel):
    document_id: UUID
    extraction_id: UUID
    items: list[CorrectionSummary]
    total: int
    limit: int
    offset: int
    has_more: bool


class ReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    failed_job_id: UUID


class ReprocessReceipt(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    request_id: UUID
    document_id: UUID
    failed_job_id: UUID
    new_job_id: UUID
    created_at: datetime
