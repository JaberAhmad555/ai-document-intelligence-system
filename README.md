# AI-Powered Document Intelligence System

A document intelligence platform being developed for financial documents, invoices, receipts, scanned files, and spreadsheets.

The completed system will extract structured financial information, validate calculations, compare documents, support manual corrections, and provide keyword and semantic search through a FastAPI backend and React dashboard.

## Current scope

The current implementation is the **Phase 1A backend foundation**:

- Multiple-file uploads.
- Format-specific content validation.
- Bounded request buffering and chunk-based file saving.
- Generated storage keys and SHA-256 checksums.
- Durable local file storage.
- SQLite document records and persistent processing jobs.
- Paginated document listing and metadata retrieval.
- Structured errors and configurable CORS.
- Alembic migrations.
- Service, API, configuration, failure-handling, migration, and legacy XLS tests.

Accepted documents remain `queued` with zero processing progress. The separate worker, OCR, AI extraction, financial validation, search, and frontend are later phases.

## Verification record

The project owner reported this checkpoint after Phase 1A Batch 5:

| Check | Reported result |
|---|---|
| Automated tests | 69 passed, 0 failed |
| Remaining deprecation warnings | 1 |
| Alembic schema check | No new upgrade operations detected |
| API health | HTTP 200 |
| Real CSV upload | HTTP 202 |
| SQLite document retrieval | Successful |

This is a historical checkpoint. Additional tests were subsequently supplied in the final completion batch; run the commands below to establish the current result.

No OCR accuracy, extraction accuracy, production throughput, or evaluation measurements are claimed.

## Architecture

Current implementation:

```text
Client / Swagger UI
        |
        v
FastAPI
  - CORS allowlist
  - HTTP upload size guard
  - Multipart limits
        |
        v
Upload service
  - Normalize display filenames
  - Stream files to local storage
  - Validate contents
  - Calculate SHA-256
        |
        +---------------------------+
        |                           |
        v                           v
Local document storage        SQLAlchemy transaction
                              - Document records
                              - Queued jobs
                                      |
                                      v
                                    SQLite
```

The database enforces valid statuses, numeric bounds, unique storage keys,
job-to-document foreign keys, retry limits, and at most one active job per
document.

Lease and heartbeat fields prepare the schema for a future worker. Worker
claiming, execution, retries, and abandoned-job recovery are not implemented
in Phase 1A.

Planned deployment:

```text
React + Vite frontend on Vercel
                |
                v
FastAPI service on Railway
        |                  |
        v                  v
PostgreSQL          Private S3-compatible storage
        ^                  ^
        |                  |
        +---- Independent Railway worker
```

The production API and worker must share durable database and document
storage. Production uploads must not depend on an ephemeral service
filesystem.

## Technology stack

| Area | Technology |
|---|---|
| Backend | Python, FastAPI |
| Settings and schemas | Pydantic, pydantic-settings |
| Database | SQLAlchemy, SQLite locally, PostgreSQL planned |
| Migrations | Alembic |
| Format validation | PyMuPDF, Pillow, openpyxl, xlrd, Python standard library |
| Tests | pytest, pytest-cov, FastAPI TestClient |
| Synthetic XLS generation | xlwt, test-only |
| Planned OCR | Tesseract |
| Planned AI extraction | Google Gemini through google-genai |
| Planned semantic search | Sentence Transformers |
| Planned frontend | React, Vite |

The existing implementation was user-verified on Windows 11 with Python
3.13.5. The final completion tests must also be run in that environment.

## Repository layout

```text
ai-document-intelligence-system/
├── .gitignore
├── README.md
└── backend/
    ├── .env.example
    ├── requirements.txt
    ├── requirements-test.txt
    ├── alembic.ini
    ├── alembic/
    │   ├── env.py
    │   ├── script.py.mako
    │   └── versions/
    │       └── 0001_initial_ingestion.py
    ├── app/
    │   ├── __init__.py
    │   ├── main.py
    │   ├── api/
    │   ├── core/
    │   ├── models/
    │   ├── schemas/
    │   └── services/
    └── tests/
        ├── test_upload_services.py
        ├── test_api_layer.py
        ├── test_migrations.py
        ├── test_config_and_failures.py
        └── test_legacy_xls.py
```

## Local setup

Prerequisites:

- Python 3.13.
- Windows PowerShell.
- A local copy of the repository.
- Network access for package installation.

SQLite needs no separate server installation. Tesseract, Gemini
credentials, Node.js, and cloud infrastructure are not needed for Phase 1A.

From the repository root:

```powershell
Set-Location .\backend

py -3.13 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip

.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt

.\.venv\Scripts\python.exe -m pip check
```

Reuse an existing virtual environment instead of recreating it.

`requirements-test.txt` includes `requirements.txt` and adds the pinned XLS
fixture writer. Application-only installation can use `requirements.txt`.
The application does not import `xlwt`.

These are bounded requirements, not an exact dependency lockfile.

Create the local environment file without overwriting an existing one:

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Process environment variables override `backend/.env`.

Important configuration:

| Setting | Purpose |
|---|---|
| `DATABASE_URL` | Local SQLite or future PostgreSQL connection |
| `STORAGE_BACKEND` | Currently `local` only |
| `LOCAL_STORAGE_PATH` | Durable local upload directory |
| `MAX_UPLOAD_FILES` | Maximum files per request |
| `MAX_FILE_SIZE_MB` | Per-file size limit in MiB |
| `MAX_UPLOAD_TOTAL_MB` | Combined file-content limit in MiB |
| `UPLOAD_CHUNK_SIZE_BYTES` | Storage copy chunk size |
| `MAX_ARCHIVE_ENTRIES` | XLSX archive entry limit |
| `MAX_ARCHIVE_UNCOMPRESSED_MB` | XLSX decompression size limit |
| `MAX_ARCHIVE_COMPRESSION_RATIO` | XLSX compression-ratio limit |
| `MAX_IMAGE_PIXELS` | Pixel limit across all image frames |
| `CORS_ORIGINS` | JSON array of permitted browser origins |
| `JOB_MAX_ATTEMPTS` | Retry limit stored on each queued job |
| `JOB_LEASE_SECONDS` | Configuration reserved for the future worker |

See [backend/.env.example](backend/.env.example) for all settings.

Relative storage and SQLite paths resolve against `backend/`.

## Database migrations

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head

.\.venv\Scripts\python.exe -m alembic -c alembic.ini current

.\.venv\Scripts\python.exe -m alembic -c alembic.ini check
```

The application never creates tables automatically at startup.

The initial revision is `0001_initial_ingestion`.

The configuration uses Alembic's `path_separator = os`, supported from
Alembic 1.16. See the [Alembic documentation](https://alembic.sqlalchemy.org/en/latest/tutorial.html#editing-the-ini-file).

An intentional downgrade is destructive:

```powershell
.\.venv\Scripts\python.exe -m alembic -c alembic.ini downgrade base
```

It removes document and job records but does not delete uploaded files.
Stop application processes and preserve needed data before downgrading.

Routine downgrade verification belongs in the automated temporary-database
tests. Do not use `stamp head` to bypass unexplained schema differences.

## Run the API

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

This is a local development command.

- [Swagger UI](http://127.0.0.1:8000/docs)
- [ReDoc](http://127.0.0.1:8000/redoc)
- [OpenAPI schema](http://127.0.0.1:8000/openapi.json)
- [Health endpoint](http://127.0.0.1:8000/api/health)

Stop the server with `Ctrl+C`.

## Supported uploads

| Format | Extensions | Current validation |
|---|---|---|
| PDF | `.pdf` | Header, parser and page structure |
| JPEG | `.jpg`, `.jpeg` | Format, structure, decoding and pixel limits |
| PNG | `.png` | Format, structure, decoding and pixel limits |
| TIFF | `.tif`, `.tiff` | Frame decoding and aggregate pixel limits |
| Excel Open XML | `.xlsx` | ZIP integrity, decompression limits, XML safety and workbook loading |
| Excel 97–2003 | `.xls` | Compound-file signature and workbook validation |
| CSV | `.csv` | Encoding, control characters, delimiters and column consistency |

Encrypted PDFs and PDFs requiring parser repair are rejected. CSV supports
UTF-8 and BOM-marked UTF-16. Single-column CSV is valid.

These checks do not perform OCR or financial extraction. They do not
constitute antivirus scanning or guarantee every downstream extraction
operation will succeed.

The default per-file limit is 25 MiB. Large PDFs may need a higher
configuration after resource testing; page count does not determine size.

## API endpoints

| Method | Endpoint | Behavior |
|---|---|---|
| GET | `/api/health` | API process liveness |
| POST | `/api/documents/upload` | Store validated files and queue jobs |
| GET | `/api/documents` | Paginated document listing |
| GET | `/api/documents/{document_id}` | Metadata and processing status |

Upload requests use `multipart/form-data` with repeated file fields named
`files`. Successful uploads return HTTP `202`.

For ordinary validation failures, the upload batch is rejected and stored
files are cleaned up. If a commit outcome cannot be determined, potentially
committed originals are retained and the response explicitly reports
`upload_outcome_unknown`.

Pagination supports `limit` from 1 to 100 and nonnegative `offset`.
Defaults are 20 and 0. Results are ordered newest first by creation time
and document ID.

Health HTTP `200` establishes API liveness only, not database readiness,
worker availability, or extraction success.

Application errors use:

```json
{
  "error": {
    "code": "document_not_found",
    "message": "The requested document does not exist.",
    "details": {}
  }
}
```

Common error statuses are `400`, `404`, `413`, `415`, `422`, and `503`.
CORS preflight responses use the CORS middleware's standard format.

## Tests and synthetic fixtures

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v --cov=app --cov-report=term-missing
```

Run the completion tests separately:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config_and_failures.py tests/test_legacy_xls.py -v
```

Review warnings without suppressing them:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -ra -W default
```

Tests cover:

- Supported formats and malformed files.
- File, batch, and HTTP request limits.
- Upload rollback and local cleanup.
- Configuration precedence, validation, URL normalization, and CORS settings.
- Stream, disk, database, cleanup, and parser-resource failures.
- Conservative handling of an unknown commit outcome.
- Queued document and job records.
- API listing, retrieval, errors, and pagination.
- Migration consistency, constraints, indexes, and downgrade/upgrade.
- Successful legacy XLS upload against an Alembic-created database.

`test_legacy_xls.py` generates a real two-sheet XLS workbook in memory.
`xlrd` independently verifies its contents before a separate API test
uploads it. The data is synthetic; no binary fixture needs to be committed.

`xlwt==1.3.0` is an older library used only for this fixture generation.
It is not part of document handling in the application.

Testing boundaries:

- Local storage and SQLite tests execute real operations in temporary locations.
- Failure-injection tests deliberately mock selected operations.
- PostgreSQL coverage currently generates migration SQL offline.
- Live PostgreSQL, OCR, AI extraction, and evaluation tests belong to later phases.
- Test results must come from executed runs; generated tests are not proof of success.

## Security and Git hygiene

Never commit:

- `.env` or credentials.
- Virtual environments.
- SQLite databases.
- Uploaded business documents.
- Temporary extraction/OCR files.
- `node_modules`.
- Confidential sample data.

Review `.env.example` and synthetic fixtures before staging.

`.gitignore` does not remove files already tracked by Git. Inspect tracked
ignored files and the staged diff before committing.

The current API is intended for local development and is not authenticated.
Do not expose it publicly. CORS is not authentication.

Production work must add protected document endpoints, private object
storage, environment-managed secrets, appropriate resource limits, and
separate API and worker services. Backend secrets must never appear in
frontend `VITE_` variables.

Request buffering, multipart parsing, and stored uploads can temporarily
consume multiple copies of the file's size. Concurrent upload capacity and
timeouts require deployment-level controls.

Filesystem operations and database commits do not form a distributed
transaction. Crash recovery and orphan reconciliation remain future work.

## Phase boundaries and remaining assignment work

The Phase 1A implementation provides the upload-to-queue foundation.
The final completion batch adds the remaining package initializers,
configuration/failure tests, and successful legacy XLS test.

Phase completion still requires a successful full regression run, review
of remaining warnings, and repository safety checks.

Later phases must deliver:

- Separate worker, PDF text extraction, OCR, preprocessing, page/sheet provenance and recovery.
- Gemini extraction, validated invoice schemas, evidence and uncertainty handling.
- Decimal financial validation, duplicate detection and document comparison.
- Corrections, retries, extracted results, keyword search and semantic search.
- React/Vite dashboard connected to the actual API.
- Labeled evaluation data and real measured metrics.
- Complete architecture/schema documentation, prompts and AI disclosure.
- Railway/Vercel deployment configuration and secured demonstration.
- Final requirement audit and recorded walkthrough.

A passing Phase 1A suite does not establish completion of the full assignment.

## AI-assisted development disclosure

OpenAI Codex assisted with architecture, code generation, tests, migrations,
and documentation.

The project owner manually copied files, installed dependencies, ran tests,
and reported observed results. Generated code remains subject to human
review and verification.

The test checkpoint above was reported by the project owner. No invented
accuracy, processing-time, or benchmark results are presented.

Expanded disclosure and development prompts will accompany the final
submission.

## First repository milestone

Suggested commit message:

```text
feat: initialize document intelligence backend
```

Create the milestone only after the full test suite and migration check
pass and the staged files have been reviewed.

No repository URL or successful GitHub push is claimed by this document.