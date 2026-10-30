# Reproducible labeled evaluation set

Five **synthetic** documents with independently authored ground truth. No medical, personal or proprietary content.

| Filename | Input form | Invoice number | Expected total |
|---|---|---|---:|
| `eval_csv_invoice.csv` | CSV | EVAL-CSV-601 | BDT 840.00 |
| `eval_digital_invoice.pdf` | native text PDF | EVAL-PDF-602 | BDT 1,575.00 |
| `eval_scanned_invoice.pdf` | image-only PDF requiring OCR | EVAL-OCR-603 | BDT 1,365.00 |
| `eval_image_invoice.png` | PNG image requiring OCR | EVAL-IMG-604 | BDT 1,050.00 |
| `eval_spreadsheet_invoice.xlsx` | XLSX workbook | EVAL-XLS-605 | BDT 1,050.00 |

All expected header fields and first line-item fields are in `labels.json`. Labels were created with the fixtures, **before any model prediction**. The scanned PDF has no selectable native PDF text; the PNG is image-only.

## Run on Windows PowerShell

From repository root:

```powershell
py -3.13 tools/evaluate_live.py --validate-only
py -3.13 tools/evaluate_live.py --limit 5  # prompts privately for token
```

Or omit the environment variable; the Python script prompts securely without echoing characters. Use `--limit 3` for a smaller evaluation. By default it targets the documented assessment backend. **Each upload invokes actual Gemini processing and could incur charges.** It also writes evaluation fixtures into the ephemeral demo DB. Do not run the same corpus repeatedly without a reason, because duplicates may affect validation warnings.

After execution, inspect `evaluation/results/live-evaluation.md` and `live-evaluation.json`. These are generated from live endpoint responses and must be included in the final submission only if the run really succeeded. Their fields include per-case status, per-field accuracy, completion times, completion/failure/timeouts, review-required flags, provider and model.

**Known limitations:** one line item per sample, small N, clear synthetic layouts, no handwritten/1,000-page stress set. A failed or timed-out case stays counted as not completed. See `docs/evaluation.md` for formulas and prior manual smoke observations.
