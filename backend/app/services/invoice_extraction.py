"""Bounded invoice extraction over the existing ReadingUnit contract.

This module performs no database transactions and does not change job status.
The worker adapter must supply a lease checkpoint and persist the returned
result through the existing fenced-write mechanism.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from app.schemas.invoice import (
    HEADER_FIELDS,
    LINE_FIELDS,
    Invoice,
    InvoiceResult,
    SourceEvidence,
    ValidationIssue,
    VerifiedEvidence,
)
from app.services.invoice_provider import (
    GeminiInvoiceProvider,
    InvoiceProvider,
    InvoiceSettings,
    encode_chunk,
)
from app.services.invoice_validation import (
    missing_invoice_fields,
    validate_invoice,
)
from app.services.reading_types import DocumentSource, ReadingUnit
from app.workers.jobs import ProcessingError


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _unit_text(unit: ReadingUnit) -> str:
    """Use actual reader output, including supplied spreadsheet cell values."""
    pieces: list[str] = []

    if unit.text:
        pieces.append(unit.text)

    if unit.cells:
        for cell in unit.cells:
            if cell.value is not None:
                pieces.append(f"{cell.address}: {cell.value}")
            if cell.formula:
                pieces.append(
                    f"{cell.address}: formula={cell.formula} "
                    "(formula text; not an evaluated value)"
                )

    if not pieces:
        if unit.native_text:
            pieces.append(unit.native_text)
        if unit.ocr_text:
            pieces.append(unit.ocr_text)

    return "\n".join(pieces)


def _field_value(invoice: Invoice, path: str) -> Any:
    if path in HEADER_FIELDS:
        return getattr(invoice, path)

    parts = path.split(".")
    if (
        len(parts) == 3
        and parts[0] == "line_items"
        and parts[1].isdigit()
        and parts[2] in LINE_FIELDS
    ):
        index = int(parts[1])
        if index < len(invoice.line_items):
            return getattr(invoice.line_items[index], parts[2])

    return None


def _set_null(invoice: Invoice, path: str) -> None:
    if path in HEADER_FIELDS:
        setattr(invoice, path, None)
        return

    _, index, name = path.split(".")
    setattr(invoice.line_items[int(index)], name, None)


def _all_value_paths(invoice: Invoice) -> Iterator[str]:
    yield from HEADER_FIELDS
    for index in range(len(invoice.line_items)):
        for name in LINE_FIELDS:
            yield f"line_items.{index}.{name}"


def _remap_path(path: str, offset: int) -> str:
    if not path.startswith("line_items."):
        return path
    _, index, name = path.split(".")
    return f"line_items.{int(index) + offset}.{name}"


def _check_evidence(
    invoice: Invoice,
    units: list[ReadingUnit],
    texts: dict[int, str],
) -> tuple[Invoice, list[VerifiedEvidence], list[ValidationIssue]]:
    clean = invoice.model_copy(deep=True)
    positions = {
        unit.sequence: unit.position
        for unit in units
    }
    supported_paths: set[str] = set()
    verified: list[VerifiedEvidence] = []
    issues: list[ValidationIssue] = []
    seen: set[tuple[str, int, str]] = set()

    for evidence in invoice.evidence:
        source_text = texts.get(evidence.sequence)
        key = (evidence.field, evidence.sequence, evidence.quote)

        valid = (
            source_text is not None
            and _field_value(invoice, evidence.field) is not None
            and _normalized(evidence.quote) in _normalized(source_text)
        )

        if not valid:
            issues.append(
                ValidationIssue(
                    code="invalid_evidence",
                    severity="warning",
                    field=evidence.field,
                    message=(
                        "A supplied evidence quote or source reference "
                        "could not be verified."
                    ),
                )
            )
            continue

        if key in seen:
            continue

        seen.add(key)
        supported_paths.add(evidence.field)
        verified.append(
            VerifiedEvidence(
                **evidence.model_dump(),
                position=positions[evidence.sequence],
            )
        )

    for path in _all_value_paths(clean):
        value = _field_value(clean, path)
        if value is not None and path not in supported_paths:
            issues.append(
                ValidationIssue(
                    code="unsupported_value",
                    severity="warning",
                    field=path,
                    message=(
                        "The proposed value had no verifiable source quote "
                        "and was withheld from extracted fields."
                    ),
                    actual=str(value),
                )
            )
            _set_null(clean, path)

    clean.evidence = [
        SourceEvidence(
            field=item.field,
            sequence=item.sequence,
            quote=item.quote,
        )
        for item in verified
    ]

    return clean, verified, issues


def extract_invoice(
    units: Iterable[ReadingUnit],
    *,
    settings: InvoiceSettings | None = None,
    provider: InvoiceProvider | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> InvoiceResult:
    """Extract one document without collecting all its input units in memory.

    A caller may pass context.ensure_active as checkpoint. It runs while
    consuming input and immediately before and after each provider request.

    An exception means no complete InvoiceResult exists. Callers must not
    publish an earlier chunk as a successfully extracted invoice.
    """
    options = settings or InvoiceSettings()
    adapter = provider or GeminiInvoiceProvider(options)
    check = checkpoint or (lambda: None)

    if adapter.name not in {"gemini", "test"}:
        raise ValueError("Provider must identify itself as gemini or test.")

    source: DocumentSource | None = None
    input_count = 0
    chunk_count = 0
    records: list[dict[str, object]] = []
    batch: list[ReadingUnit] = []
    texts: dict[int, str] = {}

    merged = Invoice(line_items_complete=True)
    issues: list[ValidationIssue] = []
    verified: list[VerifiedEvidence] = []
    conflicting_headers: set[str] = set()

    def consume_chunk() -> None:
        nonlocal chunk_count

        if not records:
            return

        if chunk_count >= options.max_chunks:
            raise ProcessingError(
                "invoice_chunk_limit",
                "The document requires more invoice extraction requests "
                "than the configured limit.",
                retryable=False,
            )

        check()
        extracted = adapter.extract(encode_chunk(records))
        check()

        if not isinstance(extracted, Invoice):
            raise ProcessingError(
                "invoice_provider_contract",
                "The invoice provider returned an invalid result type.",
                retryable=False,
            )

        clean, references, chunk_issues = _check_evidence(
            extracted,
            batch,
            texts,
        )

        offset = len(merged.line_items)
        if offset + len(clean.line_items) > options.max_line_items:
            raise ProcessingError(
                "invoice_line_item_limit",
                "The invoice exceeds the configured line-item limit.",
                retryable=False,
            )

        for issue in chunk_issues:
            issues.append(
                issue.model_copy(
                    update={"field": _remap_path(issue.field, offset)}
                )
            )

        for name in HEADER_FIELDS:
            new_value = getattr(clean, name)
            old_value = getattr(merged, name)

            if name in conflicting_headers or new_value is None:
                continue

            if old_value is None:
                setattr(merged, name, new_value)
            elif old_value != new_value:
                conflicting_headers.add(name)
                setattr(merged, name, None)
                issues.append(
                    ValidationIssue(
                        code="conflicting_field",
                        severity="warning",
                        field=name,
                        message=(
                            "Different document chunks support conflicting "
                            "values. The combined field is null pending review."
                        ),
                        expected=str(old_value),
                        actual=str(new_value),
                    )
                )

        merged.line_items.extend(clean.line_items)
        merged.line_items_complete = (
            merged.line_items_complete and clean.line_items_complete
        )
        merged.uncertain_fields.extend(
            _remap_path(path, offset)
            for path in clean.uncertain_fields
        )

        for reference in references:
            remapped = reference.model_copy(
                update={"field": _remap_path(reference.field, offset)}
            )
            verified.append(remapped)
            merged.evidence.append(
                SourceEvidence(
                    field=remapped.field,
                    sequence=remapped.sequence,
                    quote=remapped.quote,
                )
            )

        chunk_count += 1

    for unit in units:
        check()

        if not isinstance(unit, ReadingUnit):
            raise ProcessingError(
                "invoice_input_invalid",
                "Invoice extraction requires validated reading units.",
                retryable=False,
            )

        input_count += 1
        if input_count > options.max_input_units:
            raise ProcessingError(
                "invoice_input_limit",
                "The document exceeds the invoice input-unit limit.",
                retryable=False,
            )

        if unit.sequence != input_count:
            raise ProcessingError(
                "invoice_sequence_invalid",
                "Reading units must be supplied once in contiguous sequence.",
                retryable=False,
            )

        if source is None:
            source = unit.source
        elif unit.source != source:
            raise ProcessingError(
                "invoice_source_mismatch",
                "Invoice extraction cannot combine different source documents.",
                retryable=False,
            )

        text = _unit_text(unit)
        if not text.strip():
            continue

        record: dict[str, object] = {
            "sequence": unit.sequence,
            "position": unit.position.model_dump(mode="json"),
            "text": text,
        }

        if len(encode_chunk([record])) > options.chunk_characters:
            raise ProcessingError(
                "invoice_unit_too_large",
                "A source unit exceeds the invoice chunk limit. "
                "Increase INVOICE_CHUNK_CHARACTERS within model limits.",
                retryable=False,
            )

        candidate_records = [*records, record]
        if records and (
            len(records) >= options.units_per_chunk
            or len(encode_chunk(candidate_records)) > options.chunk_characters
        ):
            consume_chunk()
            records = []
            batch = []
            texts = {}

        records.append(record)
        batch.append(unit)
        texts[unit.sequence] = text

    consume_chunk()
    check()

    if source is None or chunk_count == 0:
        raise ProcessingError(
            "invoice_input_empty",
            "No readable source content was supplied for invoice extraction.",
            retryable=False,
        )

    # Revalidate the combined result, including collection bounds.
    merged.uncertain_fields = sorted(set(merged.uncertain_fields))
    merged = Invoice.model_validate(merged.model_dump())
    issues.extend(validate_invoice(merged, options.tolerance))

    review_required = bool(issues)

    return InvoiceResult(
        source=source,
        provider=adapter.name,
        model=adapter.model,
        invoice=merged,
        issues=issues,
        missing_fields=missing_invoice_fields(merged),
        review_required=review_required,
        confidence=(
            "review_required" if review_required else "source_supported"
        ),
        evidence=verified,
        input_units=input_count,
        chunks_processed=chunk_count,
    )
