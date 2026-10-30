# Manual corrections and failed-job reprocessing

This change adds real review commands to the existing FastAPI, SQLAlchemy,
Alembic, durable worker, and React application. It does not add another queue,
change the Gemini provider, or implement semantic search or evaluation.

## Apply this package

The ZIP contains only complete new/modified repository files. Extract it at the
repository root, so backend/ and frontend/ merge with the existing directories.
Stop the API and worker first, and back up the existing database before applying
the migration. Do not extract into backend/ or delete existing repository files.
Dependencies and lockfiles are unchanged. No installation is needed if the
existing environments are already installed.

From the repository root, in Windows PowerShell:

~~~powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
.\.venv\Scripts\python.exe -m alembic -c alembic.ini current
.\.venv\Scripts\python.exe -m alembic -c alembic.ini check
.\.venv\Scripts\python.exe -m pytest tests/test_review_integration.py -q
.\.venv\Scripts\python.exe -m pytest tests -q --cov=app --cov-report=term-missing
Set-Location ..\frontend
npm run test -- src/test/review.test.tsx
npm run verify
Set-Location ..
~~~

The expected migration revision is 0004_invoice_review. The commands above must
actually pass before this batch is called verified. If INVOICE_RUN_LIVE_TESTS=1
is set, the existing backend suite also performs its opted-in billable Gemini
smoke test. The new review tests always use the controlled test provider.

If dependencies have not been installed, use the existing requirements/lockfile:

~~~powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
Set-Location ..\frontend
npm ci
Set-Location ..
~~~

Restart the API and worker in separate PowerShell terminals, each from backend/:

~~~powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
~~~

~~~powershell
.\.venv\Scripts\python.exe -m app.workers --processor app.services.invoice_pipeline:process_document
~~~

From frontend/, run npm run dev for local review or use the verified production
build through your existing deployment process. Apply the database migration
before restarting deployed services with the new models.

For a deliberate rollback on a disposable database:

~~~powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m alembic -c alembic.ini downgrade 0003_invoice_results
Set-Location ..
~~~

Downgrade deletes human correction and retry receipt history. It retains original
invoice snapshots, extracted units, documents, and processing jobs. Do not
downgrade a database containing review history you need to preserve; restore
compatible application code when downgrading. SQLite must support native DROP
COLUMN (3.35+), as in the supported current Python 3.13 distribution.

## Correction behavior

A completed invoice can be corrected through Review & corrections in its document
workspace. Headers and line items are editable. Amounts stay exact decimal
strings; blanks explicitly become null. Replacing line items requires an explicit
line-items-complete declaration. Changes need a reviewer label and a reason.

Every accepted change creates an append-only invoice_corrections revision and
updates a separate effective projection on document_extractions in the same
transaction. Original invoice_result, invoice_digest, invoice_identity, timestamps,
source metadata, and extracted units are never overwritten. The application
provides no correction-update/delete endpoint. This is application-level history,
not tamper-proof storage against a database administrator.

The original financial validator is run with INVOICE_AMOUNT_TOLERANCE (existing
default 0.02). Invalid types, dates, amounts, unknown fields, empty changes and
no-op edits are rejected. Financial inconsistencies are accepted as explicit
validation issues, so a reviewer can record an accurate partial correction
without the server inventing balancing amounts. The tolerance is saved in history.

Evidence for edited values is removed only from the effective result; the original
result and quotations remain available through Show original extraction. Replacing
line items clears their effective quotations to avoid associating old row indexes
with new values. Uncertainty on untouched fields and unrelated extraction warnings
remains. A human correction is not a new Gemini output or formal approval.

reviewer_label is self-reported. These routes use the repository's existing access
boundary; they do not add authentication or verify a person's identity. Protect
document review endpoints with the same access controls as your document API.
The existing confidence enum stays compatible: corrected results remain
review_required even when arithmetic checks are clear. The UI distinguishes this
from a financial error.

Duplicate candidates and structured-field search use effective corrected values.
Original extracted text remains searchable as source evidence. Clearing supplier
or invoice number does not fall back to the original duplicate identity.

## Retry behavior

GET document metadata exposes failed_job_id only for an eligible terminal failure.
Retry failed job submits that ID, then refreshes actual server status. Queued,
running, and completed jobs cannot be manually retried. Automatic retry-wait jobs
remain controlled by the existing worker.

An accepted manual retry creates a new queued processing_jobs row with attempt
count zero and the current JOB_MAX_ATTEMPTS budget. It never resets the failed job
or its attempts, error, or timestamps. A reprocessing_requests receipt associates
the failed job, new job, document, and request ID. The old worker lease stays
invalid. The normal worker still performs claims, heartbeat, reading, Gemini
extraction, fenced persistence, and atomic completion. Existing extraction
attempts remain stored; nothing is published while the document is queued.

For local storage, a missing original is rejected before queueing. Source integrity
and downstream processing errors remain the existing worker's responsibility.

## Concurrent requests and safe resends

Each command is one short database transaction. SQLite starts BEGIN IMMEDIATE
before reading; PostgreSQL serializes mutations using a document row lock. No
provider call, OCR, or long-running processing occurs inside these transactions.
The existing partial unique active-job index remains in place.

Corrections carry extraction_id and expected_revision. Two different requests
against the same revision yield one successful revision and one HTTP 409 conflict.
The draft is retained in the UI until the reviewer explicitly reloads/cancels.

Both commands carry a UUID request_id. The same request ID and same command return
the same persisted receipt, including after a subsequent revision or job failure.
Reusing that ID with different data is a conflict. The browser retains the request
ID when explicitly resending an unchanged form after a timeout. POST requests are
never retried automatically. Server restarts do not lose idempotency receipts.

A retry is bound to a specific failed_job_id. A delayed retry cannot restart a
newer failed job accidentally. Each failed job can be consumed by one manual retry.

## API additions

All routes are under /api/documents/{document_id}. UUIDs below are fields, not
constants to copy from examples. OpenAPI documents the typed request bodies.

| Method and suffix | Response / meaning |
|---|---|
| POST /corrections | 200 CorrectionReceipt; append a revision or return its existing receipt |
| GET /corrections?extraction_id=UUID&limit=5&offset=0 | Paginated revision metadata; limit 1–20 |
| GET /corrections/{revision}?extraction_id=UUID | One revision's before/after values and issues recorded at save |
| POST /reprocess | 202 ReprocessReceipt; enqueue or return an existing receipt |

Correction request fields:

- request_id: UUID for this exact submission.
- extraction_id: UUID from GET /invoice.
- expected_revision: integer from correction_revision, initially zero.
- reviewer_label: trimmed 1–100 character self-reported label.
- reason: trimmed 1–1000 character explanation.
- changes: only invoice_number, invoice_date, supplier_name, customer_name,
  currency, subtotal, tax, total, line_items, line_items_complete. Omitted fields
  are unchanged. Amounts and quantities are decimal strings or null; JSON numbers
  are rejected. Dates are YYYY-MM-DD or null. line_items and line_items_complete
  cannot be null. Line objects have description, quantity, unit_price, line_total.

CorrectionReceipt fields: id, request_id, document_id, extraction_id, revision,
created_at. History items add reviewer_label, reason, changed_fields, original_digest
and tolerance. History detail adds corrected_fields, before_values, after_values,
and issues_at_save. History pages include items, total, limit, offset, has_more,
document_id and extraction_id. History can reference an older extraction belonging
to the same document.

Retry request fields: request_id and failed_job_id, both UUIDs. ReprocessReceipt
fields: request_id, document_id, failed_job_id, new_job_id, created_at. It records
acceptance, not the job's current state; retrieve document metadata for that.

GET /invoice now adds correction_revision, corrected_fields and original_result
(null before correction). result is the effective invoice with current duplicate
checks. Original result retains its original validation snapshot.

GET /validation adds correction_revision and accepts an optional correction_revision
query parameter. A mismatch returns 409 correction_changed, preventing the UI from
combining invoice fields and validation from different review revisions. Existing
extraction_id binding is retained. GET /content and upload contracts are unchanged.
GET document metadata adds nullable failed_job_id; list/upload records leave it null.

Errors retain the existing {error: {code, message, details}} shape. Important codes
include correction_changed, extraction_changed, idempotency_conflict, no_changes,
job_not_retryable, source_unavailable and concurrent_change. Normal input failures
are 422; missing resources 404; stale/conflicting mutations 409. Actual received
JSON bytes are bounded before parsing (4 MiB corrections; 4 KiB retry), with 413
for oversized bodies, including missing/incorrect declared lengths. Compressed
mutation bodies are rejected.

## Configuration and deployment

No new environment variables or packages are needed. Review uses the existing
DATABASE_URL, INVOICE_AMOUNT_TOLERANCE, JOB_MAX_ATTEMPTS and local storage settings.
The React client continues to use VITE_API_BASE_URL. No Gemini credentials enter
the browser. The existing CORS configuration already permits GET, POST and
Content-Type, so no CORS change is needed.

Use the same tolerance configuration on API and worker services. The migration
and locking logic target SQLite and PostgreSQL. Live PostgreSQL concurrency and
Railway/Vercel deployment still need execution-based verification.

## Verification record for this delivery

No pytest, Vitest, production build, migration, live Gemini, or deployment tests
were executed by the assistant. The existing passing baseline was reported by the
project owner; it is not a result for these changes. The delivery was reviewed as
source and checked for changed-file-only packaging.

The added backend tests exercise upload -> controlled provider -> persistence ->
correction -> retrieval, invalid inputs, preserved original values/evidence,
financial disagreements, nulls, history pagination, search/duplicate changes,
concurrent requests, idempotent replay, rollback injection, stale worker fencing,
failed-job reprocessing through the existing worker, migration round trips and
pre-parse request size limits. The frontend tests exercise real request contracts
through a mocked transport and review/retry UI behavior with synthetic fixtures.
These are repeatable tests, not measured evaluation metrics or real LLM outputs.

Before acceptance, run both full suites and the production build, then manually:

1. Open a completed synthetic invoice, correct a total, and inspect recalculated
   issues, before/after history, original extraction, and source content.
2. Open the same invoice in two tabs, save in one, then attempt a save in the stale
   tab; expect a conflict and retained draft.
3. Use an existing genuinely failed synthetic document, fix its underlying cause,
   click Retry failed job, and watch the existing worker publish its new result.
4. Confirm queued/processing/completed documents do not offer a retry action.

## Git safety and commit

The .gitignore update excludes Astra/Care Guide handoff ZIPs. Stage only the files
listed in the package manifest below. Do not stage the ZIP itself, .env files,
databases, uploads, virtual environments, or node_modules.

~~~powershell
git status --short
git diff --check
git check-ignore backend/.env frontend/.env careguide-final-fixes-context.zip
~~~

After tests pass and staged files are reviewed:

~~~powershell
$reviewFiles = @(
    '.gitignore',
    'backend/alembic/versions/0004_invoice_review.py',
    'backend/app/api/documents.py',
    'backend/app/api/review.py',
    'backend/app/main.py',
    'backend/app/models/extraction.py',
    'backend/app/schemas/documents.py',
    'backend/app/schemas/review.py',
    'backend/app/services/invoice_pipeline.py',
    'backend/app/services/review.py',
    'backend/tests/test_review_integration.py',
    'docs/review-and-reprocessing.md',
    'frontend/src/DocumentDetail.tsx',
    'frontend/src/ReviewControls.tsx',
    'frontend/src/api.ts',
    'frontend/src/contracts.ts',
    'frontend/src/review.css',
    'frontend/src/test/App.test.tsx',
    'frontend/src/test/fixtures.ts',
    'frontend/src/test/review.test.tsx'
)
git add -- $reviewFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add audited invoice corrections and safe failed-job retries"
git push
~~~

No commit, push, or deployment is claimed by this delivery. The implementation and
tests were AI-assisted; the project owner must report their actual review and
executed verification honestly.

## Changed/new file manifest

- MODIFIED — .gitignore
- NEW — backend/alembic/versions/0004_invoice_review.py
- MODIFIED — backend/app/api/documents.py
- NEW — backend/app/api/review.py
- MODIFIED — backend/app/main.py
- MODIFIED — backend/app/models/extraction.py
- MODIFIED — backend/app/schemas/documents.py
- NEW — backend/app/schemas/review.py
- MODIFIED — backend/app/services/invoice_pipeline.py
- NEW — backend/app/services/review.py
- NEW — backend/tests/test_review_integration.py
- NEW — docs/review-and-reprocessing.md
- MODIFIED — frontend/src/DocumentDetail.tsx
- NEW — frontend/src/ReviewControls.tsx
- MODIFIED — frontend/src/api.ts
- MODIFIED — frontend/src/contracts.ts
- NEW — frontend/src/review.css
- MODIFIED — frontend/src/test/App.test.tsx
- MODIFIED — frontend/src/test/fixtures.ts
- NEW — frontend/src/test/review.test.tsx
