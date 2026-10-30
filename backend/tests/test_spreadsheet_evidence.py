"""Regression cases for safe spreadsheet citation repair (no Gemini network)."""

import json
from uuid import uuid4

from app.schemas.invoice import Invoice, SourceEvidence
from app.services.invoice_extraction import extract_invoice
from app.services.invoice_provider import InvoiceSettings
from app.services.reading_types import (
    CellValue, DocumentSource, ReadingUnit, SourcePosition,
)


class Provider:
    name = "test"
    model = "spreadsheet-evidence-fake"

    def __init__(self, invoice):
        self.invoice = invoice
        self.contents = None

    def extract(self, contents):
        self.contents = json.loads(contents)
        return self.invoice


def units(extension=".xlsx"):
    source = DocumentSource(
        document_id=uuid4(),
        original_filename="synthetic" + extension,
        storage_key="synthetic" + extension,
        sha256="a" * 64,
        extension=extension,
    )
    units = [
        ReadingUnit(
            sequence=1,
            source=source,
            kind="sheet",
            method="spreadsheet" if extension == ".xlsx" else "pdf_text",
            position=SourcePosition(part_number=1, part_count=1, sheet_name="Invoice"),
            text="",
        )
    ]
    for sequence, values in [(2, ["Evaluation Test"]), (3, ["Invoice Number", "EVAL-XLS-605"])]:
        cells = tuple(
            CellValue(
                column_number=i,
                address=f"{chr(64+i)}{sequence-1}",
                data_type="text", value=value,
            ) for i, value in enumerate(values, start=1)
        )
        units.append(
            ReadingUnit(
                sequence=sequence, source=source,
                kind="row", method="spreadsheet",
                position=SourcePosition(
                    part_number=1, part_count=1, sheet_name="Invoice", row_number=sequence-1,
                ),
                text=json.dumps(values), cells=cells,
            )
        )
    return units


def result_for(quote, seq=2):
    extracted = Invoice(
        invoice_number="EVAL-XLS-605",
        evidence=[SourceEvidence(field="invoice_number", sequence=seq, quote=quote)],
    )
    provider = Provider(extracted)
    result = extract_invoice(
        units(),
        settings=InvoiceSettings(_env_file=None, gemini_api_key=None, gemini_model="test"),
        provider=provider,
    )
    return result, provider


def test_spreadsheet_label_value_quote_with_neighboring_sequence_is_verified():
    result, provider = result_for("Invoice Number: EVAL-XLS-605")
    assert result.invoice.invoice_number == "EVAL-XLS-605"
    assert result.evidence[0].sequence == 3
    assert "Invoice Number: EVAL-XLS-605" in provider.contents["document_units"][1]["text"]
    assert any(i.code == "evidence_sequence_repaired" for i in result.issues)
    assert not any(i.code == "unsupported_value" for i in result.issues)


def test_spreadsheet_invented_quote_is_still_rejected():
    result, _ = result_for("There was no such text in any cell")
    assert result.invoice.invoice_number is None
    assert result.evidence == []
    assert any(i.code == "invalid_evidence" for i in result.issues)


def test_correct_spreadsheet_sequence_needs_no_repair():
    result, _ = result_for("B2: EVAL-XLS-605", seq=3)
    assert result.invoice.invoice_number == "EVAL-XLS-605"
    assert not any(i.code == "evidence_sequence_repaired" for i in result.issues)


def test_wrong_field_value_is_not_rescued_by_a_neighboring_quote():
    invoice = Invoice(
        invoice_number="HALLUCINATED-605",
        evidence=[SourceEvidence(
            field="invoice_number", sequence=2,
            quote="Invoice Number: EVAL-XLS-605",
        )],
    )
    provider = Provider(invoice)
    result = extract_invoice(
        units(),
        settings=InvoiceSettings(_env_file=None, gemini_api_key=None, gemini_model="test"),
        provider=provider,
    )
    assert result.invoice.invoice_number is None
    assert any(i.code == "invalid_evidence" for i in result.issues)
