import { z } from 'zod';

export const statuses = ['queued', 'processing', 'completed', 'failed'] as const;
export const extensions = ['.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.xlsx', '.xls', '.csv'] as const;
export const statusSchema = z.enum(statuses);
export type DocumentStatus = z.infer<typeof statusSchema>;
const uuid = z.string().uuid();
const decimal = z.string().regex(/^-?\d{1,16}(\.\d{1,6})?$/);
const amount = decimal.nullable();
const maybeText = z.string().nullable();

export const documentSchema = z.object({
  id: uuid, original_filename: z.string(), extension: z.string(), media_type: z.string(),
  size_bytes: z.number().int().nonnegative(), sha256: z.string(), status: statusSchema,
  processing_stage: z.string(), progress_percent: z.number().int().min(0).max(100),
  total_units: z.number().int().nonnegative().nullable(), processed_units: z.number().int().nonnegative(),
  error_code: maybeText, error_message: maybeText, created_at: z.string(), updated_at: z.string(),
  processing_started_at: maybeText, processing_finished_at: maybeText,
});
export type DocumentRecord = z.infer<typeof documentSchema>;
export const documentListSchema = z.object({
  items: z.array(documentSchema), total: z.number().int().nonnegative(),
  limit: z.number().int().positive(), offset: z.number().int().nonnegative(), has_more: z.boolean(),
});
export type DocumentList = z.infer<typeof documentListSchema>;
export const uploadSchema = z.object({ count: z.number().int().positive(), documents: z.array(documentSchema) });
export type UploadResponse = z.infer<typeof uploadSchema>;

export const sourceSchema = z.object({
  document_id: uuid, original_filename: z.string(), sha256: z.string(), extension: z.string(),
});
export const positionSchema = z.object({
  part_number: z.number().int().positive(), part_count: z.number().int().positive(),
  page_number: z.number().int().positive().nullable(), frame_number: z.number().int().positive().nullable(),
  sheet_name: maybeText, row_number: z.number().int().positive().nullable(),
  physical_line_start: z.number().int().positive().nullable(), physical_line_end: z.number().int().positive().nullable(),
});
export type Position = z.infer<typeof positionSchema>;
const evidenceSchema = z.object({ field: z.string(), sequence: z.number().int().positive(), quote: z.string() });
export const verifiedEvidenceSchema = evidenceSchema.extend({ position: positionSchema });
export type Evidence = z.infer<typeof verifiedEvidenceSchema>;
export const issueSchema = z.object({
  code: z.string(), severity: z.enum(['warning', 'error']), field: z.string(), message: z.string(),
  expected: maybeText, actual: maybeText, related_document_id: uuid.nullable(),
});
export type Issue = z.infer<typeof issueSchema>;
export const invoiceSchema = z.object({
  invoice_number: maybeText, invoice_date: maybeText, supplier_name: maybeText, customer_name: maybeText,
  currency: maybeText, subtotal: amount, tax: amount, total: amount,
  line_items: z.array(z.object({ description: maybeText, quantity: amount, unit_price: amount, line_total: amount })),
  line_items_complete: z.boolean(), uncertain_fields: z.array(z.string()), evidence: z.array(evidenceSchema),
});
const confidence = z.enum(['source_supported', 'review_required']);
export const invoiceResponseSchema = z.object({
  document_id: uuid, extraction_id: uuid, created_at: z.string(), duplicates_checked_at: z.string(), duplicates_truncated: z.boolean(),
  result: z.object({
    schema_version: z.literal('1'), source: sourceSchema, provider: z.enum(['gemini', 'test']), model: z.string(),
    invoice: invoiceSchema, issues: z.array(issueSchema), missing_fields: z.array(z.string()),
    review_required: z.boolean(), confidence, evidence: z.array(verifiedEvidenceSchema),
    input_units: z.number().int().positive(), chunks_processed: z.number().int().positive(),
  }),
});
export type InvoiceResponse = z.infer<typeof invoiceResponseSchema>;
export const validationSchema = z.object({
  document_id: uuid, extraction_id: uuid, issues: z.array(issueSchema), missing_fields: z.array(z.string()),
  uncertain_fields: z.array(z.string()), review_required: z.boolean(), confidence,
  duplicates_checked_at: z.string(), duplicates_truncated: z.boolean(),
});
export type ValidationResponse = z.infer<typeof validationSchema>;
export const contentSchema = z.object({
  document_id: uuid, extraction_id: uuid, source: sourceSchema,
  items: z.array(z.object({
    sequence: z.number().int().positive(), kind: z.enum(['page', 'image_frame', 'sheet', 'row']),
    method: z.enum(['pdf_text', 'ocr', 'pdf_text_and_ocr', 'spreadsheet', 'csv']),
    text: z.string(), native_text: maybeText, ocr_text: maybeText, position: positionSchema,
    cells: z.array(z.object({
      column_number: z.number().int().positive(), address: z.string(),
      data_type: z.enum(['text', 'number', 'date', 'boolean', 'formula', 'error', 'blank']),
      value: maybeText, formula: maybeText,
    })), warnings: z.array(z.string()),
  })),
  total_units: z.number().int().nonnegative(), limit: z.number().int().positive(),
  after_sequence: z.number().int().nonnegative(), next_after_sequence: z.number().int().nonnegative().nullable(), has_more: z.boolean(),
}).refine(page => !page.has_more || (page.next_after_sequence !== null && page.next_after_sequence > page.after_sequence), {
  message: 'Invalid content cursor',
});
export type ContentPage = z.infer<typeof contentSchema>;
