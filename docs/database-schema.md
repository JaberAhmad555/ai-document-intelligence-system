# Relational database schema

**Database:** SQLite for local development and the Render Free demo. SQLAlchemy mappings allow a future PostgreSQL transition, but migrations and operations must be verified there independently. Schema is defined by `backend/app/models/{ingestion,extraction}.py` and Alembic revisions `0001`–`0004`.

## Entity relationship diagram

```mermaid
erDiagram
 DOCUMENTS ||--o{ PROCESSING_JOBS : queues
 DOCUMENTS ||--o{ DOCUMENT_EXTRACTIONS : produces
 PROCESSING_JOBS ||--o| DOCUMENT_EXTRACTIONS : produces
 DOCUMENT_EXTRACTIONS ||--o{ EXTRACTED_UNITS : contains
 DOCUMENTS ||--o| DOCUMENT_EXTRACTION_HEADS : publishes
 DOCUMENT_EXTRACTIONS ||--o{ INVOICE_CORRECTIONS : corrected_by
 DOCUMENTS ||--o{ REPROCESSING_REQUESTS : retry_requests

 DOCUMENTS {
  uuid id PK
  string original_filename
  string storage_key
  string storage_backend
  string extension
  bigint size_bytes
  string sha256
  string status
  string processing_stage
  int progress_percent
  timestamp created_at
 }
 PROCESSING_JOBS {
  uuid id PK
  uuid document_id FK
  string status
  int attempt_count
  timestamp available_at
  string locked_by
  uuid lease_token
  timestamp lease_expires_at
  string last_error_code
 }
 DOCUMENT_EXTRACTIONS {
  uuid id PK
  uuid document_id FK
  uuid job_id FK
  int attempt_number
  json source
  string source_sha256
  json invoice_result
  int correction_revision
  json corrected_result
 }
 EXTRACTED_UNITS {
  uuid extraction_id FK
  int sequence PK
  string method
  text text
  json position
  json cells
  json warnings
 }
 DOCUMENT_EXTRACTION_HEADS {
  uuid document_id PK
  uuid extraction_id FK
 }
 INVOICE_CORRECTIONS {
  uuid id PK
  uuid document_id
  uuid extraction_id
  int revision
  uuid request_id
  string reviewer_label
  text reason
  json before_values
  json after_values
  timestamp created_at
 }
 REPROCESSING_REQUESTS {
  uuid request_id PK
  uuid document_id FK
  uuid failed_job_id FK
  uuid new_job_id FK
  timestamp created_at
 }
```

The diagram is a conceptual ER overview, not a complete DDL dump. Exact foreign keys, unique constraints, indexes, field sizes and nullable conditions are defined in Alembic and SQLAlchemy source.

## Table responsibilities

| Table | Main purpose |
|---|---|
| `documents` | Original metadata, checksum, storage reference, state, progress and errors |
| `processing_jobs` | Durable queue lifecycle, attempts, errors, lease and heartbeat fields |
| `document_extractions` | Processing attempt, source digest, normalized Gemini result and correction snapshot |
| `extracted_units` | Text and source positions (page/sheet/row), OCR/native text, cell metadata, warnings |
| `document_extraction_heads` | Latest published extraction for each document |
| `invoice_corrections` | Audited revisions with reason and original/corrected field values |
| `reprocessing_requests` | Idempotent retry receipt; links prior failed job and new job |

## Persistence and integrity

All invoice amounts are validated as exact decimal strings in API schemas, not binary floats. Job claims and corrections use concurrency rules and transaction boundaries; corrections preserve previous results and an append-only audit history. Source-unit sequence IDs support evidence references. Duplicate checking uses normalized supplier/invoice identifiers across records and may produce warnings rather than deleting invoices.

## Schema verification commands

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
```

**Reported local result:** `0004_invoice_review (head)` and `No new upgrade operations detected` on October 30, 2026. **Reported Render deployment:** migration `0003 → 0004` ran on commit `9234118`.
