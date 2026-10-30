# Extraction evaluation: observations and repeatable protocol

**Status: preliminary, not a final accuracy benchmark.** This document separates previously observed production smoke tests from a forthcoming reproducible labeled dataset evaluation. Do not fill in unknown latency or success-rate numbers without actually running the evaluator.

## A. Previously observed production smoke tests (October 30, 2026)

The project owner inspected results on the deployed Vercel + Render site using synthetic documents. These observations were recorded in screenshots during the assessment; they are **not** the same as an independently repeated, timed, statistically representative benchmark.

| Format and case | Expected total | Observed total | Processing | Automated validation | Observed limitation |
|---|---:|---:|---|---|---|
| One-row CSV `test-invoice-complete.csv` | BDT 1,575.00 | BDT 1,575.00 | Completed | 0 errors / 0 warnings | Synthetic, easy layout |
| Digital PDF `invoice_text_pdf_test.pdf` | BDT 2,520.00 | BDT 2,520.00 | Completed | 0 errors / 0 warnings | Single-page synthetic PDF |
| Image-only scanned PDF `Scanned_Invoice_OCR_Test.pdf` | BDT 1,365.00 | BDT 1,365.00 | Completed | 0 errors / 2 warnings | Line-item quantity uncertain / missing |
| Duplicate CSV (same supplier + number) | BDT 1,575.00 | BDT 1,575.00 | Completed | 1 duplicate warning | Checks a known duplicate, not full cross-file reconciliation |

On these **three unique invoice layouts**, all three displayed header total amounts matched their labeled expected totals (3/3, 100% *total-field agreement within this tiny convenience sample*). This **must not** be presented as overall field-extraction accuracy: date/party/amount/line-item correctness across diverse invoices, false positives and complete-case accuracy require broader labeled scoring.

## B. Automated software tests (separate from model quality)

The owner reported running 315 passing backend tests (6 skipped), 55 passing frontend tests, successful TypeScript/Vite build, a clean Alembic schema check at revision `0004_invoice_review`, and `npm audit` returning 0 known findings on October 30, 2026. These prove that tested code paths met their assertions; they are **not** extraction-accuracy, throughput or stress benchmarks.

## C. New reproducible labeled test set

See [`../evaluation/README.md`](../evaluation/README.md) and `../evaluation/labels.json`. Five synthetic documents exercise CSV, digital PDF, scanned PDF, PNG image and XLSX ingestion. Ground truth is human-authored by construction, not generated from the model's own predictions. Document names, expected values and labels are committed alongside each fixture.

**Run the evaluator** with the actual deployed API and a private bearer token to obtain actual **field accuracy, mean/end-to-end processing latency, p50/p95, document completion rate and failure rate**. It measures upload acknowledgement to terminal document status using the real network, queue and Gemini provider. Results depend on Render cold starts, Gemini availability and test load. Use the tool-generated JSON and Markdown as the final measured report, with timestamp, model and sample size.

```powershell
cd "C:\Users\User\OneDrive\Desktop\AI-Document-Intelligence-Starter\ai-document-intelligence-system"
$env:DOCUMENT_BASE_URL = "https://ai-document-intelligence-api-cjh6.onrender.com"
py -3.13 tools/evaluate_live.py --limit 5  # prompts privately for token
```

Only run against the authorized assessment service. Five documents each trigger a Gemini processing request and may incur API charges. No personal data is used. Outputs are saved under `evaluation/results/` for inspection; the final report is generated only from live responses.

### Metric definitions

- **Field accuracy** = number of exact normalized matches to known, non-null expected fields / number of known, non-null expected fields attempted. Numeric amounts use decimal equality; dates use ISO strings; text uses whitespace collapse/casefold. `null`/missing predictions are incorrect when a field is expected.
- **Document success rate** = number of documents reaching completed status / number accepted for evaluation; failure rate is terminal failures or timeouts / accepted. A timeout does not necessarily mean terminal backend failure; report counts separately.
- **End-to-end processing time** = seconds from successful upload acknowledgement to observation of completed/failed status. Includes polling interval and queue/network time; distinguish this from model-only latency.
- **p50/p95** = nearest-rank percentiles of observed completed document durations. With N=5, p95 effectively reflects the maximum and is not a stable population estimate.
- **Uncertainty/review** = number of completed documents with `review_required=true`, and warning/error counts. This is not identical to extraction error.

### Validity and limitations

A small synthetic corpus cannot establish real-world business accuracy, robustness on poor-quality scans or operation on 1,000-page files. A production evaluation should add diverse supplier templates, multi-sheet spreadsheets, handwriting, partial scans, invoices with missing fields, corrupted files, intentionally inconsistent arithmetic, negative duplicate pairs, and both PDF/PDF and PDF/Excel reconciliation labels. Report confidence intervals when sample size permits; test without human corrections and include error analysis. Do **not** call the current keyword search semantic search.
