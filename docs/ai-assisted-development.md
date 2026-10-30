# AI-assisted development disclosure and prompt record

This take-home was developed with substantive AI assistance. **OpenAI ChatGPT** helped plan the work, explain architecture and deployment, interpret logs, diagnose bugs, suggest tests and prepare documentation. **OpenAI Codex Astra** generated significant portions of backend/frontend implementation and changes as source ZIP batches; the owner applied them and ran verification commands. **Google Gemini** is used **inside the running application** as an invoice-data extraction service, not as the developer of the source repository.

## Representative prompts and decisions

This is a representative record; it is not a claim to contain every prompt, internal model interaction or source edit.

| Phase | Prompt / instruction theme | Human validation reported |
|---|---|---|
| Backend foundation | Build a FastAPI document upload API, SQLAlchemy and Alembic migrations, safe storage and processing queue | Local API reachable, upload accepted, SQLite retrieval, migration and tests |
| PDF/OCR and worker | Implement real readers (PDF/Tesseract/CSV/Excel), durable job processing, bounded batches and source provenance | Reader and worker tests; Python test suites |
| Gemini extraction | Implement typed invoice schema, evidence-grounded extraction and financial validation | A Gemini `400 INVALID_ARGUMENT` schema rejection was diagnosed and fixed by removing problematic `maxItems` from **outbound** schema only; Pydantic still validates inbound results |
| React dashboard | “Create a complete, production-quality React + TypeScript frontend using Vite,” using the **actual FastAPI contract**; no mock production data | 26 initial frontend tests and production build, then real browser invoice screenshots |
| Human review and reprocessing | “Manual correction of extracted invoice values ... persisted revision history ... failed-job reprocessing ...” | 273 backend tests / 40 frontend tests reported after this batch |
| Access protection | “Protect ALL document-related APIs ... constant-time token ... frontend access screen ... memory only ...” | 315 backend / 55 frontend tests; Render commit `9234118` Live; owner confirmed login and unauthenticated `401` |
| Documentation and evaluation | Source-derived architecture, README updates, labeled fixtures and evaluation methodology | Generated documentation must be reviewed before submission; live evaluation metrics only valid if evaluator is run |

## AI-related engineering risks and responses

- AI-generated code was not assumed correct: unexecuted Codex batches were distinguished from locally executed tests, TypeScript builds, migration checks, audits and observed deployed behavior.
- Gemini occasionally returned service errors and initially rejected the structured invoice schema. The deployment uses a model actually validated by a direct request and a live invoice integration test.
- Model outputs are validated, source evidence quoted and attached to source units, and low-confidence OCR quantities flagged for human review. These steps reduce risk but **do not guarantee no hallucinations**.
- The demo uses a shared bearer token rather than user-specific authorization; confidential records must not be uploaded, and activity labels are not proof of individual reviewer identity.
- The hosted demo has ephemeral storage and is not advertised as production durable.

## Candidate accountability

The candidate must review this disclosure and the repository, confirm the final results and share real CV/portfolio/reference information. Authorship must not be falsely presented as entirely handwritten. Any screenshots, benchmark numbers and claimed timings must be grounded in actual executions. Secret values (Gemini API key and reviewer token) are not included in this disclosure.
