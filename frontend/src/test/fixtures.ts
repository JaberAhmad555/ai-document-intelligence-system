/** Synthetic test data only. This module is never imported by production code. */
import { vi } from 'vitest';
import type { Api } from '../api';
import type { ContentPage, DocumentList, DocumentRecord, InvoiceResponse, ValidationResponse } from '../contracts';

export const ID = '11111111-1111-4111-8111-111111111111';
export const OTHER_ID = '22222222-2222-4222-8222-222222222222';
export const EXTRACTION = '33333333-3333-4333-8333-333333333333';
export function documentFixture(changes: Partial<DocumentRecord> = {}): DocumentRecord {
  return { id: ID, original_filename: 'synthetic-review.pdf', extension: '.pdf', media_type: 'application/pdf', size_bytes: 2048, sha256: 'a'.repeat(64),
    status: 'completed', processing_stage: 'completed', progress_percent: 100, total_units: 8, processed_units: 8,
    error_code: null, error_message: null, created_at: '2026-10-30T08:00:00Z', updated_at: '2026-10-30T08:02:00Z',
    processing_started_at: '2026-10-30T08:01:00Z', processing_finished_at: '2026-10-30T08:02:00Z', ...changes };
}
export function listFixture(items = [documentFixture()]): DocumentList { return { items, total: items.length, limit: 20, offset: 0, has_more: false }; }
export function positionFixture(page = 3) { return { part_number: page, part_count: 8, page_number: page, frame_number: null, sheet_name: null, row_number: null, physical_line_start: null, physical_line_end: null }; }
export function invoiceFixture(id = ID): InvoiceResponse {
  return { document_id: id, extraction_id: EXTRACTION, created_at: '2026-10-30T08:02:00Z', duplicates_checked_at: '2026-10-30T08:03:00Z', duplicates_truncated: false,
    result: { schema_version: '1', source: { document_id: id, original_filename: 'synthetic-review.pdf', sha256: 'a'.repeat(64), extension: '.pdf' }, provider: 'test', model: 'synthetic-fixture',
      invoice: { invoice_number: 'TEST-001', invoice_date: '2026-10-30', supplier_name: 'Synthetic Supplier', customer_name: null, currency: 'USD', subtotal: '100.00', tax: '10.00', total: '120.00',
        line_items: [{ description: 'Synthetic consulting', quantity: '2', unit_price: '50.00', line_total: '100.00' }], line_items_complete: true,
        uncertain_fields: ['supplier_name'], evidence: [{ field: 'total', sequence: 3, quote: 'Total: 120.00' }] },
      issues: [
        { code: 'grand_total_mismatch', severity: 'error', field: 'total', message: 'Synthetic arithmetic mismatch.', expected: '110.00', actual: '120.00', related_document_id: null },
        { code: 'missing_field', severity: 'warning', field: 'customer_name', message: 'Customer not supplied.', expected: null, actual: null, related_document_id: null },
        { code: 'possible_duplicate', severity: 'warning', field: 'invoice_number', message: 'Synthetic duplicate candidate.', expected: null, actual: 'TEST-001', related_document_id: OTHER_ID },
      ], missing_fields: ['customer_name'], review_required: true, confidence: 'review_required',
      evidence: [{ field: 'total', sequence: 3, quote: 'Total: 120.00', position: positionFixture() }], input_units: 8, chunks_processed: 1,
    } };
}
export function validationFixture(id = ID): ValidationResponse {
  const response = invoiceFixture(id);
  return { document_id: id, extraction_id: EXTRACTION, issues: response.result.issues, missing_fields: ['customer_name'], uncertain_fields: ['supplier_name'],
    review_required: true, confidence: 'review_required', duplicates_checked_at: response.duplicates_checked_at, duplicates_truncated: false };
}
export function contentFixture(after = 0, id = ID): ContentPage {
  const sequence = after + 1;
  return { document_id: id, extraction_id: EXTRACTION, source: invoiceFixture(id).result.source,
    items: sequence <= 8 ? [{ sequence, kind: 'page', method: sequence === 3 ? 'ocr' : 'pdf_text', text: sequence === 3 ? 'Total: 120.00\nSynthetic source text.' : 'Synthetic page ' + sequence,
      native_text: null, ocr_text: sequence === 3 ? 'Total: 120.00\nSynthetic source text.' : null,
      position: positionFixture(sequence), cells: [], warnings: sequence === 3 ? ['Synthetic OCR warning for test.'] : [] }] : [],
    total_units: 8, limit: 5, after_sequence: after, next_after_sequence: sequence < 8 ? sequence : null, has_more: sequence < 8 };
}
export function fakeApi() {
  return {
    documents: vi.fn<Api['documents']>().mockResolvedValue(listFixture()),
    document: vi.fn<Api['document']>().mockImplementation(async id => documentFixture({ id })),
    upload: vi.fn<Api['upload']>().mockResolvedValue({ count: 1, documents: [documentFixture({ status: 'queued', processing_stage: 'queued', progress_percent: 0, total_units: null, processed_units: 0, processing_started_at: null, processing_finished_at: null })] }),
    invoice: vi.fn<Api['invoice']>().mockImplementation(async id => invoiceFixture(id)),
    validation: vi.fn<Api['validation']>().mockImplementation(async id => validationFixture(id)),
    content: vi.fn<Api['content']>().mockImplementation(async (id, query) => contentFixture(query.after_sequence, id)),
  };
}
export const limitsFixture = { count: 10, fileBytes: 25 * 1024 * 1024, totalBytes: 100 * 1024 * 1024 };
