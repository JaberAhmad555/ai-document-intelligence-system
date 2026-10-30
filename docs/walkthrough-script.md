# Walkthrough recording — 5 to 7 minute script

Record your screen with narration. Use synthetic documents only; do not display the Render environment-variable values, browser devtools Authorization headers, private credentials, or a real patient's/customer's data. Keep Render's Free service awake before recording. If a queue takes time, explain honestly rather than editing out errors and misrepresenting the timing.

**0:00–0:35 | Context.** “This is my AI-Powered Document Intelligence System for Care Guide. It ingests PDFs, scanned PDFs, images and spreadsheets, extracts invoice fields using Gemini, validates arithmetic and sources, and supports a human review workflow.” Show the GitHub README, then dashboard.

**0:35–1:05 | Architecture.** Open `docs/architecture.md` Mermaid diagram. Explain React → authenticated FastAPI → queued background worker → OCR/reader → Gemini → validation/SQLite. Explicitly mention this is a free-demo deployment with temporary storage.

**1:05–1:40 | Access and document inbox.** Show password page, enter credentials privately (hide keyboard entry), open the inbox. Show search/filter, list status and processing progress.

**1:40–2:35 | Document upload and extraction.** Upload a synthetic CSV or digital PDF. Explain HTTP 202 means queued; wait until processing ends. Show supplier, customer, date, subtotal, tax, total and line items. Emphasize that amounts are strings internally and validated numerically on the server.

**2:35–3:20 | Evidence + OCR.** Open source evidence, show page/row reference. Show an image-only invoice and its OCR result. Explain the quantity review warning rather than pretending OCR is flawless.

**3:20–4:10 | Duplicate and correction.** Upload a duplicate or open an existing pair, show the duplicate warning and related document. Edit a synthetic invoice field, add a clear reason, save and show revision history. Avoid edits that would confuse reviewers about the original totals.

**4:10–4:35 | Failure/retry.** Show the retry UI using a safely prepared failed synthetic job or the automated test demonstration, *only if a failed job is available*. Do not intentionally break the live worker merely to manufacture a screenshot.

**4:35–5:15 | Tests + metrics.** Show actual `pytest`/Vitest results (315 backend and 55 frontend at reported checkpoint), then `evaluation/results/` if you executed the live evaluator. If not run, do not claim measured extraction accuracy or latency.

**5:15–6:00 | Limitations and decisions.** Say clearly: keyword search is implemented; semantic search and full PDF/PDF or PDF/Excel reconciliation are not implemented; the Render Free deployment has ephemeral SQLite/local uploads. Explain production improvement plan (PostgreSQL, durable object storage, independent workers, RBAC/rate limits). Mention AI-assisted development disclosure.

**6:00–6:15 | Closing.** Show source repository, live frontend, and documentation folder; finish with a brief summary. Export MP4 (screen capture plus voice narration) and share using a link with recruiter access enabled. Check link in an incognito window.

**Recording checklist:** 1080p if available, browser zoom readable, no notification popups, no credentials in frame, mic audible, final MP4 opens normally, share permissions checked.
