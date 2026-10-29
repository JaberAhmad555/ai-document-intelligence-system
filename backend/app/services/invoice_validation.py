"""Financial checks without changing extracted source values."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, localcontext
from uuid import UUID

from app.schemas.invoice import (
    HEADER_FIELDS,
    Invoice,
    ValidationIssue,
    parse_decimal,
)


def missing_invoice_fields(invoice: Invoice) -> list[str]:
    return [
        name
        for name in HEADER_FIELDS
        if getattr(invoice, name) is None
    ]


def validate_invoice(
    invoice: Invoice,
    tolerance: Decimal = Decimal("0.02"),
) -> list[ValidationIssue]:
    tolerance = parse_decimal(tolerance)
    if tolerance < 0:
        raise ValueError("Tolerance cannot be negative.")

    issues: list[ValidationIssue] = []

    for name in missing_invoice_fields(invoice):
        issues.append(
            ValidationIssue(
                code="missing_field",
                severity="warning",
                field=name,
                message=f"The document does not provide a supported {name}.",
            )
        )

    for name in sorted(set(invoice.uncertain_fields)):
        issues.append(
            ValidationIssue(
                code="uncertain_field",
                severity="warning",
                field=name,
                message="The extracted value needs human confirmation.",
            )
        )

    def compare(
        code: str,
        field: str,
        expected: Decimal,
        actual: Decimal,
        message: str,
    ) -> None:
        if abs(expected - actual) > tolerance:
            issues.append(
                ValidationIssue(
                    code=code,
                    severity="error",
                    field=field,
                    message=message,
                    expected=str(expected),
                    actual=str(actual),
                )
            )

    with localcontext() as context:
        context.prec = 64

        if (
            invoice.subtotal is not None
            and invoice.tax is not None
            and invoice.total is not None
        ):
            compare(
                "grand_total_mismatch",
                "total",
                invoice.subtotal + invoice.tax,
                invoice.total,
                "Subtotal plus tax differs from the stated total. "
                "Check discounts, shipping, withholding, or other adjustments.",
            )

        for index, item in enumerate(invoice.line_items):
            prefix = f"line_items.{index}"

            for name in ("quantity", "unit_price", "line_total"):
                if getattr(item, name) is None:
                    issues.append(
                        ValidationIssue(
                            code="missing_line_item_field",
                            severity="warning",
                            field=f"{prefix}.{name}",
                            message="A line-item arithmetic input is missing.",
                        )
                    )

            if (
                item.quantity is not None
                and item.unit_price is not None
                and item.line_total is not None
            ):
                compare(
                    "line_item_mismatch",
                    f"{prefix}.line_total",
                    item.quantity * item.unit_price,
                    item.line_total,
                    "Quantity multiplied by unit price differs from the "
                    "stated line total. Check line discounts or included tax.",
                )

            for name in ("quantity", "unit_price", "line_total"):
                value = getattr(item, name)
                if value is not None and value < 0:
                    issues.append(
                        ValidationIssue(
                            code="negative_value",
                            severity="warning",
                            field=f"{prefix}.{name}",
                            message="Confirm whether this is a credit or return.",
                            actual=str(value),
                        )
                    )

        for name in ("subtotal", "tax", "total"):
            value = getattr(invoice, name)
            if value is not None and value < 0:
                issues.append(
                    ValidationIssue(
                        code="negative_value",
                        severity="warning",
                        field=name,
                        message="Confirm whether this is a credit document.",
                        actual=str(value),
                    )
                )

        if not invoice.line_items:
            issues.append(
                ValidationIssue(
                    code="line_items_missing",
                    severity="warning",
                    field="line_items",
                    message="No supported invoice line items were extracted.",
                )
            )
        elif not invoice.line_items_complete:
            issues.append(
                ValidationIssue(
                    code="line_items_incomplete",
                    severity="warning",
                    field="line_items",
                    message="Line-item completeness could not be confirmed.",
                )
            )
        elif (
            invoice.subtotal is not None
            and all(
                item.line_total is not None
                for item in invoice.line_items
            )
        ):
            line_sum = sum(
                (
                    item.line_total
                    for item in invoice.line_items
                    if item.line_total is not None
                ),
                Decimal("0"),
            )
            compare(
                "subtotal_mismatch",
                "subtotal",
                line_sum,
                invoice.subtotal,
                "The sum of stated line totals differs from the subtotal. "
                "Check whether line totals include tax or adjustments.",
            )

    return issues


def duplicate_key(invoice: Invoice) -> tuple[str, str] | None:
    """Conservative supplier-plus-invoice-number candidate identity."""
    if not invoice.invoice_number or not invoice.supplier_name:
        return None

    def normalize(value: str) -> str:
        return " ".join(value.split()).casefold()

    return (
        normalize(invoice.supplier_name),
        normalize(invoice.invoice_number),
    )


def find_duplicate_issues(
    invoice: Invoice,
    candidates: Iterable[tuple[UUID, Invoice]],
    *,
    current_document_id: UUID | None = None,
) -> list[ValidationIssue]:
    """Flag candidates; a match does not prove a document is fraudulent."""
    key = duplicate_key(invoice)
    if key is None:
        return []

    issues: list[ValidationIssue] = []
    seen: set[UUID] = set()

    for document_id, other in candidates:
        if document_id == current_document_id or document_id in seen:
            continue
        if duplicate_key(other) != key:
            continue

        seen.add(document_id)
        issues.append(
            ValidationIssue(
                code="possible_duplicate",
                severity="warning",
                field="invoice_number",
                message=(
                    "Another document has the same normalized supplier "
                    "and invoice number."
                ),
                actual=invoice.invoice_number,
                related_document_id=document_id,
            )
        )

        if (
            invoice.currency is not None
            and other.currency is not None
            and invoice.currency != other.currency
        ):
            issues.append(
                ValidationIssue(
                    code="duplicate_currency_mismatch",
                    severity="warning",
                    field="currency",
                    message="Potential duplicate documents state different currencies.",
                    expected=other.currency,
                    actual=invoice.currency,
                    related_document_id=document_id,
                )
            )

    return issues
