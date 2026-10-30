# Measured synthetic-document evaluation

> Auto-generated from **actual authenticated API responses**. This is a small, synthetic set, not a production accuracy claim.

- Evaluated at (UTC): `2026-10-30T16:13:43.550447+00:00`
- Backend: `https://ai-document-intelligence-api-cjh6.onrender.com`
- Dataset: `Care Guide synthetic invoices v1`

## Metrics

| Metric | Measured value |
|---|---:|
| Accepted documents | 5/5 |
| Completed | 5 |
| Terminal failed | 0 |
| Timed out | 0 |
| Client errors | 0 |
| Completion rate (accepted) | 100.0% |
| Terminal failure rate (accepted) | 0.0% |
| Completed-only field accuracy | 100.0% (60/60) |
| All-accepted field accuracy | 100.0% (60/60) |
| Mean end-to-end completion time | 9.1209 s |
| Median end-to-end completion time | 12.3845 s |
| p95 completion time | 12.4556 s |
| Documents requiring review | 0 |
| Total validation issues | 0 |

## By-file outcomes

| Synthetic file | Format | Status | Field matches | Processing seconds | Warnings |
|---|---|---|---:|---:|---:|
| `eval_csv_invoice.csv` | csv | completed | 12/12 | 12.3845 | 0 |
| `eval_digital_invoice.pdf` | digital_pdf | completed | 12/12 | 4.1496 | 0 |
| `eval_scanned_invoice.pdf` | scanned_pdf | completed | 12/12 | 12.4304 | 0 |
| `eval_image_invoice.png` | image | completed | 12/12 | 12.4556 | 0 |
| `eval_spreadsheet_invoice.xlsx` | xlsx | completed | 12/12 | 4.1845 | 0 |

## Per-field discrepancies

No field discrepancies observed among the completed samples. This does not imply zero generalization error.

## Method and limitations

Each case was uploaded with an authorized HTTP POST, then status-polled until `completed`, `failed` or timeout. Duration starts **after upload acknowledgement**, includes queue/network/poll overhead and is not pure Gemini latency. A missing expected field counts as an incorrect extraction. Numeric comparisons use `Decimal`; text is casefolded and whitespace-normalized. Each example contains one line item; item-order differences can lower this simple metric.

The dataset is only five handcrafted **synthetic** invoices. It is not representative of real-world customers or 1,000-page documents. Cross-file reconciliation, semantic search, role-based permissions and durable Render Free storage are outside this evaluation. For fuller validity, add independent natural layouts, actual reference annotations, larger samples, and multi-page stress measurements.

Source: `evaluation/fixtures/`, `evaluation/labels.json`, `tools/evaluate_live.py`, and same-run `evaluation/results/live-evaluation.json`.
