"""Synthetic invoice tests.

Most tests use an explicitly identified fake provider. They do not measure
Gemini extraction accuracy. The separately gated live test calls Gemini.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.invoice import (
    HEADER_FIELDS,
    Invoice,
    LineItem,
    SourceEvidence,
)
from app.services.invoice_extraction import extract_invoice
from app.services.invoice_provider import (
    GeminiInvoiceProvider,
    InvoiceSettings,
    parse_invoice_response,
)
from app.services.invoice_validation import (
    find_duplicate_issues,
    validate_invoice,
)
from app.services.reading_types import (
    DocumentSource,
    ReadingUnit,
    SourcePosition,
)
from app.workers.jobs import ProcessingError


TEXT = """Invoice number: SYN-001
Invoice date: 2026-10-30
Supplier: Synthetic Supplies Ltd
Customer: Example Customer Ltd
Currency: USD
Description: Consulting
Quantity: 2
Unit price: 50.00
Line total: 100.00
Subtotal: 100.00
Tax: 10.00
Total: 110.00
"""


class FakeProvider:
    name = "test"
    model = "synthetic-test-provider"

    def __init__(self, responses: list[Invoice]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def extract(self, contents: str) -> Invoice:
        self.calls.append(contents)
        if not self.responses:
            raise AssertionError("Unexpected additional provider call.")
        return self.responses.pop(0).model_copy(deep=True)


def options(**overrides: object) -> InvoiceSettings:
    values: dict[str, object] = {
        "_env_file": None,
        "gemini_api_key": None,
        "gemini_model": "test-model",
        "tolerance": Decimal("0.02"),
        "chunk_characters": 48000,
        "units_per_chunk": 32,
        "max_chunks": 128,
    }
    values.update(overrides)
    return InvoiceSettings(**values)


def source() -> DocumentSource:
    return DocumentSource(
        document_id=uuid4(),
        original_filename="synthetic-invoice.pdf",
        storage_key=f"{uuid4().hex}.pdf",
        sha256="a" * 64,
        extension=".pdf",
    )


def unit(
    text: str = TEXT,
    *,
    sequence: int = 1,
    shared_source: DocumentSource | None = None,
    pages: int = 1,
) -> ReadingUnit:
    return ReadingUnit(
        sequence=sequence,
        source=shared_source or source(),
        position=SourcePosition(
            part_number=sequence,
            part_count=pages,
            page_number=sequence,
        ),
        kind="page",
        method="pdf_text",
        text=text,
        native_text=text,
    )


def invoice(*, sequence: int = 1) -> Invoice:
    result = Invoice(
        invoice_number="SYN-001",
        invoice_date="2026-10-30",
        supplier_name="Synthetic Supplies Ltd",
        customer_name="Example Customer Ltd",
        currency="USD",
        subtotal="100.00",
        tax="10.00",
        total="110.00",
        line_items=[
            LineItem(
                description="Consulting",
                quantity="2",
                unit_price="50.00",
                line_total="100.00",
            )
        ],
        line_items_complete=True,
    )

    quotes = {
        "invoice_number": "Invoice number: SYN-001",
        "invoice_date": "Invoice date: 2026-10-30",
        "supplier_name": "Supplier: Synthetic Supplies Ltd",
        "customer_name": "Customer: Example Customer Ltd",
        "currency": "Currency: USD",
        "subtotal": "Subtotal: 100.00",
        "tax": "Tax: 10.00",
        "total": "Total: 110.00",
        "line_items.0.description": "Description: Consulting",
        "line_items.0.quantity": "Quantity: 2",
        "line_items.0.unit_price": "Unit price: 50.00",
        "line_items.0.line_total": "Line total: 100.00",
    }

    result.evidence = [
        SourceEvidence(field=field, sequence=sequence, quote=quote)
        for field, quote in quotes.items()
    ]
    return result


def codes(issues: list) -> set[str]:
    return {issue.code for issue in issues}


def test_decimal_round_trip_uses_strings() -> None:
    result = invoice()
    encoded = json.loads(result.model_dump_json())

    assert encoded["total"] == "110.00"
    restored = Invoice.model_validate_json(result.model_dump_json())
    assert restored.total == Decimal("110.00")


@pytest.mark.parametrize("value", [1.1, True, "NaN", "Infinity", "1e3", "1,000"])
def test_invalid_money_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        Invoice(total=value)


def test_correct_invoice_has_no_financial_issues() -> None:
    assert validate_invoice(invoice()) == []


def test_incorrect_total_is_reported_without_rewriting_it() -> None:
    result = invoice()
    result.total = Decimal("120.00")

    issues = validate_invoice(result)
    mismatch = next(
        issue for issue in issues if issue.code == "grand_total_mismatch"
    )

    assert mismatch.expected == "110.00"
    assert mismatch.actual == "120.00"
    assert result.total == Decimal("120.00")


def test_line_arithmetic_and_subtotal_mismatches_are_reported() -> None:
    result = invoice()
    result.line_items[0].line_total = Decimal("90.00")

    found = codes(validate_invoice(result))
    assert "line_item_mismatch" in found
    assert "subtotal_mismatch" in found


def test_tolerance_is_inclusive() -> None:
    result = invoice()
    result.total = Decimal("110.02")
    assert "grand_total_mismatch" not in codes(
        validate_invoice(result, Decimal("0.02"))
    )

    result.total = Decimal("110.03")
    assert "grand_total_mismatch" in codes(
        validate_invoice(result, Decimal("0.02"))
    )


def test_missing_fields_are_reported() -> None:
    issues = validate_invoice(Invoice())
    missing = {
        issue.field for issue in issues if issue.code == "missing_field"
    }

    assert missing == set(HEADER_FIELDS)
    assert "line_items_missing" in codes(issues)


def test_incomplete_lines_do_not_claim_subtotal_verification() -> None:
    result = invoice()
    result.line_items_complete = False
    result.line_items[0].line_total = Decimal("1.00")

    found = codes(validate_invoice(result))
    assert "line_items_incomplete" in found
    assert "subtotal_mismatch" not in found


def test_duplicate_candidate_and_currency_mismatch() -> None:
    original = invoice()
    other = invoice()
    other.supplier_name = "  SYNTHETIC   SUPPLIES LTD "
    other.currency = "EUR"
    document_id = uuid4()

    issues = find_duplicate_issues(
        original,
        [(document_id, other), (document_id, other)],
    )

    assert codes(issues) == {
        "possible_duplicate",
        "duplicate_currency_mismatch",
    }
    assert all(issue.related_document_id == document_id for issue in issues)


def test_duplicate_detection_requires_supplier_and_invoice_number() -> None:
    incomplete = Invoice(invoice_number="SYN-001")
    assert find_duplicate_issues(
        incomplete,
        [(uuid4(), invoice())],
    ) == []


def test_duplicate_detection_excludes_current_document() -> None:
    document_id = uuid4()
    assert find_duplicate_issues(
        invoice(),
        [(document_id, invoice())],
        current_document_id=document_id,
    ) == []


def test_structured_response_parsing() -> None:
    parsed = parse_invoice_response(invoice().model_dump_json(), 100000)
    assert parsed.total == Decimal("110.00")


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        '{"total": 10.5}',
        '{"total": "10.00", "unexpected": true}',
    ],
)
def test_invalid_provider_output_is_rejected(response: str) -> None:
    with pytest.raises(ProcessingError):
        parse_invoice_response(response, 100000)


def test_provider_response_size_limit() -> None:
    with pytest.raises(ProcessingError):
        parse_invoice_response("x" * 101, 100)


def test_missing_credentials_fail_explicitly() -> None:
    with pytest.raises(ProcessingError):
        GeminiInvoiceProvider(options(gemini_api_key=None))


def test_missing_model_fails_explicitly() -> None:
    with pytest.raises(ProcessingError):
        GeminiInvoiceProvider(
            options(gemini_api_key="synthetic-not-a-real-key", gemini_model=None)
        )


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "configured-test-model")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("INVOICE_AMOUNT_TOLERANCE", "0.05")

    configured = InvoiceSettings(_env_file=None)

    assert configured.gemini_model == "configured-test-model"
    assert configured.tolerance == Decimal("0.05")
    assert "synthetic-not-a-real-key" not in repr(configured)


@pytest.mark.parametrize("tolerance", ["-0.01", "NaN", "101"])
def test_invalid_tolerance_is_rejected(tolerance: str) -> None:
    with pytest.raises(ValidationError):
        options(tolerance=tolerance)


def test_extraction_preserves_source_and_evidence() -> None:
    reading = unit()
    fake = FakeProvider([invoice()])

    result = extract_invoice([reading], settings=options(), provider=fake)

    assert result.provider == "test"
    assert result.source == reading.source
    assert result.invoice.total == Decimal("110.00")
    assert result.review_required is False
    assert result.confidence == "source_supported"
    assert result.chunks_processed == 1
    assert result.evidence[0].position.page_number == 1

    sent = json.loads(fake.calls[0])
    assert sent["document_units"][0]["text"] == TEXT


def test_unsupported_value_becomes_null_and_is_reported() -> None:
    response = invoice()
    response.evidence = [
        evidence
        for evidence in response.evidence
        if evidence.field != "invoice_number"
    ]

    result = extract_invoice(
        [unit()],
        settings=options(),
        provider=FakeProvider([response]),
    )

    assert result.invoice.invoice_number is None
    assert "invoice_number" in result.missing_fields
    assert "unsupported_value" in codes(result.issues)
    assert result.review_required is True


def test_invented_quote_is_not_accepted() -> None:
    response = invoice()
    response.evidence = [
        evidence.model_copy(update={"quote": "A quote absent from this document"})
        if evidence.field == "total"
        else evidence
        for evidence in response.evidence
    ]

    result = extract_invoice(
        [unit()],
        settings=options(),
        provider=FakeProvider([response]),
    )

    assert result.invoice.total is None
    assert "invalid_evidence" in codes(result.issues)


def test_wrong_source_sequence_is_not_accepted() -> None:
    response = invoice(sequence=99)
    result = extract_invoice(
        [unit()],
        settings=options(),
        provider=FakeProvider([response]),
    )

    assert result.invoice.total is None
    assert result.evidence == []


def test_conflicting_chunk_values_remain_null() -> None:
    shared = source()
    second = Invoice(
        total="120.00",
        line_items_complete=True,
        evidence=[
            SourceEvidence(field="total", sequence=2, quote="Total: 120.00")
        ],
    )

    result = extract_invoice(
        [
            unit(shared_source=shared, pages=2),
            unit(
                "Total: 120.00",
                sequence=2,
                shared_source=shared,
                pages=2,
            ),
        ],
        settings=options(units_per_chunk=1),
        provider=FakeProvider([invoice(), second]),
    )

    assert result.chunks_processed == 2
    assert result.invoice.total is None
    assert "conflicting_field" in codes(result.issues)


def test_line_evidence_indexes_are_remapped_across_chunks() -> None:
    shared = source()
    second = Invoice(
        line_items=[LineItem(description="Additional service")],
        line_items_complete=False,
        evidence=[
            SourceEvidence(
                field="line_items.0.description",
                sequence=2,
                quote="Additional service",
            )
        ],
    )

    result = extract_invoice(
        [
            unit(shared_source=shared, pages=2),
            unit(
                "Additional service",
                sequence=2,
                shared_source=shared,
                pages=2,
            ),
        ],
        settings=options(units_per_chunk=1),
        provider=FakeProvider([invoice(), second]),
    )

    assert len(result.invoice.line_items) == 2
    assert any(
        evidence.field == "line_items.1.description"
        and evidence.sequence == 2
        for evidence in result.evidence
    )


def test_chunk_limit_fails_without_returning_partial_invoice() -> None:
    shared = source()

    with pytest.raises(ProcessingError):
        extract_invoice(
            [
                unit(shared_source=shared, pages=2),
                unit(
                    "Total: 110.00",
                    sequence=2,
                    shared_source=shared,
                    pages=2,
                ),
            ],
            settings=options(units_per_chunk=1, max_chunks=1),
            provider=FakeProvider([invoice()]),
        )


def test_oversized_unit_is_not_silently_truncated() -> None:
    fake = FakeProvider([])

    with pytest.raises(ProcessingError):
        extract_invoice(
            [unit("X" * 3000)],
            settings=options(chunk_characters=2000),
            provider=fake,
        )

    assert fake.calls == []


def test_different_documents_cannot_be_combined() -> None:
    with pytest.raises(ProcessingError):
        extract_invoice(
            [
                unit(pages=2),
                unit(sequence=2, pages=2),
            ],
            settings=options(),
            provider=FakeProvider([]),
        )


def test_input_must_start_at_sequence_one() -> None:
    with pytest.raises(ProcessingError):
        extract_invoice(
            [unit(sequence=2, pages=2)],
            settings=options(),
            provider=FakeProvider([]),
        )


def test_blank_input_fails_without_calling_provider() -> None:
    fake = FakeProvider([])

    with pytest.raises(ProcessingError):
        extract_invoice(
            [unit("")],
            settings=options(),
            provider=fake,
        )

    assert fake.calls == []


def test_checkpoint_failure_propagates_without_becoming_success() -> None:
    class LostLease(RuntimeError):
        pass

    def checkpoint() -> None:
        raise LostLease("Synthetic lease loss")

    fake = FakeProvider([])

    with pytest.raises(LostLease):
        extract_invoice(
            [unit()],
            settings=options(),
            provider=fake,
            checkpoint=checkpoint,
        )

    assert fake.calls == []


def test_provider_failure_does_not_fall_back_to_fabricated_results() -> None:
    class FailingProvider:
        name = "test"
        model = "failing-test-provider"

        def extract(self, contents: str) -> Invoice:
            raise ProcessingError(
                "synthetic_provider_failure",
                "Synthetic provider failure.",
                retryable=True,
            )

    with pytest.raises(ProcessingError):
        extract_invoice(
            [unit()],
            settings=options(),
            provider=FailingProvider(),
        )


@pytest.mark.skipif(
    os.getenv("INVOICE_RUN_LIVE_TESTS") != "1",
    reason="Explicit opt-in required for a real billable Gemini request.",
)
def test_live_gemini_synthetic_invoice() -> None:
    result = extract_invoice(
        [unit()],
        settings=InvoiceSettings(),
    )

    assert result.provider == "gemini"
    assert result.invoice.invoice_number == "SYN-001"
    assert result.invoice.total == Decimal("110.00")
    assert result.evidence
    assert "grand_total_mismatch" not in codes(result.issues)
