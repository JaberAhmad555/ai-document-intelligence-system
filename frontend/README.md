# Document Intelligence review console

React 19, TypeScript, Vite 7 and Zod 4 frontend for the FastAPI document intelligence backend. This README describes the **current** implementation including password access, human corrections and reprocessing (repository commit `9234118`), not the earlier frontend-only batch.

## Working features

- Demo access gate; shared token is kept in tab memory, not localStorage, cookies or a `VITE_` variable. Unauthorized responses lock the workspace.
- Multi-file drag-and-drop/file-picker upload (up to 10 files by default), progress updates, list search/filter and pagination.
- Invoice details, line items, exact decimal formatting, validation warnings, verified evidence and source-unit navigation.
- Human correction controls with required review reason and revision history; optimistic revision checks and idempotent request IDs.
- Explicit reprocess action for terminal failed jobs, without blind replay of uploads.
- Responsive layout and test coverage for API errors, authentication, dashboard operations, corrections and retry scenarios.

## Local development

Node.js 22.12+ or 24+, npm, the FastAPI backend and the background worker are required. From `frontend`:

```powershell
if (-not (Test-Path '.env')) { Copy-Item .env.example .env }
npm ci
npm.cmd run dev
```

Open `http://127.0.0.1:5173`. The default `VITE_API_BASE_URL=/` uses Vite's local development proxy to FastAPI at `http://127.0.0.1:8000`. In the **backend** `.env`, set `DOCUMENT_API_TOKEN` to a long URL-safe token and `GEMINI_API_KEY` to your own private key. Use this token in the frontend login screen. No secret should be set as `VITE_...`.

Start the backend from the backend directory in two terminals:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

```powershell
.\.venv\Scripts\python.exe -m app.workers --processor app.services.invoice_pipeline:process_document --poll-seconds 2
```

## Verification

```powershell
npm.cmd run verify
npm.cmd audit
```

The owner reported **55 passing frontend tests** and successful TypeScript/Vite build plus **0 npm audit findings** on October 30, 2026. These are frontend/API-mock regression tests; live Gemini processing requires running the backend/worker and completing a browser flow.

## Deployment

Vercel project root: `frontend`. Framework: Vite. Build: `npm run build`; output: `dist`. Set `VITE_API_BASE_URL=https://ai-document-intelligence-api-cjh6.onrender.com` without trailing `/api`. Set backend `CORS_ORIGINS` to the exact deployed frontend origin. Vite's local proxy is not deployed into production assets.

**Public demo URL:** https://ai-document-intelligence-system-rose.vercel.app

All document routes are guarded by a backend bearer token. This is shared demo access, not production user authentication. Render Free SQLite/local uploads are temporary and the service can cold-start. Upload only synthetic files.

## API integration

All requests except public liveness require `Authorization: Bearer <token>` and expose structured errors. Important routes:

- `GET /api/auth/check`
- `POST /api/documents/upload` (repeated multipart fields named `files`)
- `GET /api/documents`, `GET /api/documents/{id}`
- `GET /api/documents/{id}/invoice`, `/validation`, `/content`
- `POST /api/documents/{id}/corrections`, `GET /api/documents/{id}/corrections`, `GET /api/documents/{id}/corrections/{revision}`
- `POST /api/documents/{id}/reprocess`

API responses are validated with Zod. The browser doesn't calculate invoice amounts or fabricate missing field values; the backend supplies authoritative validation and evidence references. Authentication failures clear the in-memory session and require re-entry.

## Known limitations

- Keyword document search is present; **semantic/vector search is not**.
- Cross-file duplicate warnings are present; **full PDF/PDF and PDF/Excel reconciliation is not**.
- No end-user accounts, role-based access or independent upload quotas. Treat this as a protected assessment demonstration only.

See the [repository README](../README.md), [architecture](../docs/architecture.md), [evaluation](../docs/evaluation.md) and [AI-use disclosure](../docs/ai-assisted-development.md).
