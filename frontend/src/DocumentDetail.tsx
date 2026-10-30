import { useEffect, useState, type FormEvent } from 'react';
import { ApiError, type Api } from './api';
import type { Evidence, InvoiceResponse, ValidationResponse } from './contracts';
import { documentHref, useRemote } from './hooks';
import { CorrectionControls, RetryControl } from './ReviewControls';
import { EmptyState, ErrorPanel, Loading, Pagination, Progress, Status, fieldLabel, formatAmount, formatBytes, formatDate, positionLabel } from './components';

type Focus = { reference: Evidence; extractionId: string };

function ValidationPanel({ data }: { data: ValidationResponse }) {
  const [offset, setOffset] = useState(0);
  useEffect(() => { setOffset(0); }, [data.extraction_id, data.issues.length]);
  const errors = data.issues.filter(issue => issue.severity === 'error').length;
  return <section className="panel validation-panel" aria-labelledby="validation-title"><div className="section-heading"><div><p className="eyebrow">REVIEW</p><h2 id="validation-title">Checks & exceptions</h2></div>
    <span className={'review-badge ' + (data.review_required ? 'needs-review' : 'checks-clear')}>{(data.correction_revision ?? 0) > 0 && data.issues.length === 0 ? 'Corrected · checks clear' : data.review_required ? 'Needs review' : 'Automated checks clear'}</span></div>
    <div className="validation-summary"><span><strong>{errors}</strong> errors</span><span><strong>{data.issues.length - errors}</strong> warnings</span><span><strong>{data.missing_fields.length}</strong> missing fields</span></div>
    {data.missing_fields.length > 0 && <div className="field-flags"><span>Missing</span>{data.missing_fields.map(field => <span className="tag" key={field}>{fieldLabel(field)}</span>)}</div>}
    {data.uncertain_fields.length > 0 && <p className="inline-note">{data.uncertain_fields.length} field{data.uncertain_fields.length === 1 ? '' : 's'} flagged as uncertain. Inspect the source before relying on these values.</p>}
    {data.issues.length === 0 ? <div className="checks-note"><strong>No issues reported by these checks.</strong><p>This is an automated result, not a human approval or a guarantee of accuracy.</p></div> : <><ul className="issues-list">{data.issues.slice(offset, offset + 8).map((issue, index) => <li key={offset + index} className={'issue issue-' + issue.severity}><div className="issue-top"><span className="issue-severity">{issue.severity}</span><strong>{fieldLabel(issue.field)}</strong></div><p>{issue.message}</p>
      {(issue.expected !== null || issue.actual !== null) && <dl className="comparison-values">{issue.expected !== null && <div><dt>Expected / comparison</dt><dd>{issue.expected}</dd></div>}{issue.actual !== null && <div><dt>{(data.correction_revision ?? 0) > 0 ? 'Current value' : 'Extracted value'}</dt><dd>{issue.actual}</dd></div>}</dl>}
      {issue.related_document_id && <a className="text-link" href={documentHref(issue.related_document_id)}>Open related document ↗</a>}</li>)}</ul>
      {data.issues.length > 8 && <Pagination offset={offset} count={data.issues.slice(offset, offset + 8).length} total={data.issues.length} limit={8} hasMore={offset + 8 < data.issues.length} onOffset={setOffset} />}</>}
    <div className="panel-footnote">Duplicate candidates checked {formatDate(data.duplicates_checked_at, true)}.{data.duplicates_truncated && ' The candidate list is limited; additional matches exist.'}</div>
  </section>;
}
function InvoiceOverview({ response, onEvidence, idPrefix = '' }: { response: InvoiceResponse; onEvidence: (reference: Evidence) => void; idPrefix?: string }) {
  const [lineOffset, setLineOffset] = useState(0);
  const [evidenceOffset, setEvidenceOffset] = useState(0);
  const invoice = response.result.invoice;
  const references = response.result.evidence;
  const find = (field: string) => references.find(reference => reference.field === field);
  const sourceButton = (field: string, label: string) => {
    const reference = find(field);
    return reference ? <button className="source-link" aria-label={'View source for ' + label} onClick={() => onEvidence(reference)}>Source ↗</button> : null;
  };
  const textFields = [
    ['supplier_name', 'Supplier', invoice.supplier_name], ['customer_name', 'Customer', invoice.customer_name],
    ['invoice_number', 'Invoice number', invoice.invoice_number], ['invoice_date', 'Invoice date', invoice.invoice_date ? formatDate(invoice.invoice_date) : null],
    ['currency', 'Currency', invoice.currency],
  ] as const;
  return <>
    {response.result.provider === 'test' && <div className="notice notice-warning" role="status">Test-provider result. This document was not extracted by Gemini.</div>}
    <section className="panel invoice-overview" aria-labelledby={idPrefix + 'invoice-title'}><div className="section-heading"><div><p className="eyebrow">{(response.correction_revision ?? 0) > 0 ? 'CORRECTED INFORMATION' : 'EXTRACTED INFORMATION'}</p><h2 id={idPrefix + 'invoice-title'}>Invoice overview</h2></div><span className="subtle-label">{(response.correction_revision ?? 0) > 0 ? 'Human corrected' : response.result.provider === 'gemini' ? 'Gemini' : 'Test provider'}</span></div>
      <dl className="invoice-fields">{textFields.map(([field, label, value]) => <div key={field} className={field === 'supplier_name' || field === 'customer_name' ? 'party-field' : ''}><dt>{label}{sourceButton(field, label)}</dt><dd className={value === null ? 'not-provided' : ''}>{value ?? 'Not provided'}</dd>{invoice.uncertain_fields.includes(field) && <small className="uncertain-label">Needs confirmation</small>}</div>)}</dl>
      <div className="amount-grid">{(['subtotal', 'tax', 'total'] as const).map(field => <div key={field} className={field === 'total' ? 'amount-total' : ''}><div className="amount-label"><span>{field === 'total' ? 'Invoice total' : fieldLabel(field)}</span>{sourceButton(field, fieldLabel(field))}</div><strong className={invoice[field] === null ? 'not-provided' : ''}>{formatAmount(invoice[field], invoice.currency)}</strong></div>)}</div>
      <div className="panel-footnote">Extracted {formatDate(response.created_at, true)} · {response.result.model} · Source values are preserved.</div>
    </section>
    <section className="panel" aria-labelledby={idPrefix + 'line-items-title'}><div className="section-heading"><h2 id={idPrefix + 'line-items-title'}>Line items <span className="count">{invoice.line_items.length}</span></h2></div>
      {!invoice.line_items_complete && <p className="notice notice-warning">Line-item completeness could not be confirmed. Check the source for omitted or ambiguous rows.</p>}
      {invoice.line_items.length === 0 ? <EmptyState title="No line items extracted"><p>The result contains no supported line-item rows.</p></EmptyState> : <><div className="table-scroll"><table className="line-table"><caption className="sr-only">Extracted invoice line items</caption><thead><tr><th scope="col">Description</th><th scope="col" className="numeric">Quantity</th><th scope="col" className="numeric">Unit price</th><th scope="col" className="numeric">Line total</th><th scope="col">Evidence</th></tr></thead><tbody>{invoice.line_items.slice(lineOffset, lineOffset + 20).map((line, index) => {
        const absolute = lineOffset + index;
        const reference = references.find(item => item.field.startsWith('line_items.' + absolute + '.'));
        return <tr key={absolute}><td>{line.description ?? <span className="not-provided">Not provided</span>}</td><td className="numeric">{formatAmount(line.quantity, null, false)}</td><td className="numeric">{formatAmount(line.unit_price, invoice.currency)}</td><td className="numeric">{formatAmount(line.line_total, invoice.currency)}</td><td>{reference ? <button className="source-link" onClick={() => onEvidence(reference)} aria-label={'View source for line item ' + (absolute + 1)}>Source ↗</button> : <span className="muted">Unavailable</span>}</td></tr>;
      })}</tbody></table></div>{invoice.line_items.length > 20 && <Pagination offset={lineOffset} count={invoice.line_items.slice(lineOffset, lineOffset + 20).length} total={invoice.line_items.length} hasMore={lineOffset + 20 < invoice.line_items.length} limit={20} onOffset={setLineOffset} />}</>}
    </section>
    <section className="panel" aria-labelledby={idPrefix + 'evidence-title'}><div className="section-heading"><div><h2 id={idPrefix + 'evidence-title'}>Evidence references <span className="count">{references.length}</span></h2><p>Matched source quotations support review; they do not guarantee interpretation.</p></div></div>
      {references.length === 0 ? <p className="inline-note">No verified quotations were returned.</p> : <><ul className="evidence-list">{references.slice(evidenceOffset, evidenceOffset + 6).map((reference, index) => <li key={evidenceOffset + index}><div className="evidence-heading"><strong>{fieldLabel(reference.field)}</strong><span>{positionLabel(reference.position)}</span></div><blockquote>{reference.quote}</blockquote><button className="text-button" onClick={() => onEvidence(reference)}>Open source unit {reference.sequence} ↗</button></li>)}</ul>{references.length > 6 && <Pagination offset={evidenceOffset} count={references.slice(evidenceOffset, evidenceOffset + 6).length} total={references.length} limit={6} hasMore={evidenceOffset + 6 < references.length} onOffset={setEvidenceOffset} />}</>}
    </section>
  </>;
}
function SourceViewer({ id, api, initialVersion, focus, reload }: { id: string; api: Api; initialVersion?: string; focus: Focus | null; reload: () => void }) {
  const [cursor, setCursor] = useState(focus ? focus.reference.sequence - 1 : 0);
  const [version, setVersion] = useState(focus?.extractionId || initialVersion);
  const [history, setHistory] = useState<number[]>([]);
  const [jump, setJump] = useState(String(cursor + 1));
  const resource = useRemote({
    key: JSON.stringify(['content', id, cursor, version]),
    load: signal => api.content(id, { after_sequence: cursor, limit: 5, extraction_id: version }, signal),
  });
  useEffect(() => { if (resource.data && !version) setVersion(resource.data.extraction_id); }, [resource.data, version]);
  const page = resource.data;
  const navigate = (next: number, remember = true) => { if (remember) setHistory(current => [...current, cursor]); setCursor(next); setJump(String(next + 1)); };
  const jumpTo = (event: FormEvent) => { event.preventDefault(); const value = Number(jump); if (page && Number.isInteger(value) && value >= 1 && value <= page.total_units) navigate(value - 1); };
  const changed = resource.error instanceof ApiError && ['extraction_changed', 'document_not_completed'].includes(resource.error.code);
  return <section className="panel source-panel" aria-labelledby="source-title"><div className="section-heading"><div><p className="eyebrow">PROVENANCE</p><h2 id="source-title">Source content</h2><p>Extracted text and cell values, with their original source positions.</p></div><button className="button button-small" onClick={resource.refresh} disabled={resource.refreshing}>Refresh content</button></div>
    {focus && <div className="selected-evidence"><span className="eyebrow">SELECTED EVIDENCE · {fieldLabel(focus.reference.field)}</span><blockquote>{focus.reference.quote}</blockquote><small>{positionLabel(focus.reference.position)} · Unit {focus.reference.sequence}</small></div>}
    {resource.error && <ErrorPanel error={resource.error} retry={changed ? reload : resource.refresh} title={changed ? 'Document version changed' : 'Source content is unavailable'} />}
    {resource.loading && <Loading label="Loading source content" />}
    {page && <><div className="source-toolbar"><span>{page.items.length ? 'Units ' + page.items[0].sequence + '–' + page.items[page.items.length - 1].sequence : 'No units'} of {page.total_units}</span><form className="jump-form" onSubmit={jumpTo}><label htmlFor="source-unit">Go to unit</label><input id="source-unit" type="number" min={1} max={page.total_units} value={jump} onChange={event => setJump(event.target.value)} required /><button className="button button-small" disabled={resource.refreshing}>Go</button></form></div>
      {page.items.length === 0 && <EmptyState title="End of extracted content"><p>Choose a source unit or return to the previous page.</p></EmptyState>}
      {page.items.map(unit => <article className="source-unit" key={unit.sequence}><header><h3>{positionLabel(unit.position)}</h3><span>Unit {unit.sequence} · {unit.method.replaceAll('_', ' ')}</span></header>
        {unit.warnings.length > 0 && <div className="notice notice-warning"><strong>Extraction warnings</strong><ul>{unit.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
        <pre className="source-text" tabIndex={0} aria-label={'Extracted text for unit ' + unit.sequence}>{unit.text || [unit.native_text, unit.ocr_text].filter(Boolean).join('\n') || 'No text in this source unit.'}</pre>
        {unit.cells.length > 0 && <details className="source-details"><summary>Cell values and formulas ({unit.cells.length})</summary><div className="table-scroll"><table><caption className="sr-only">Source cells for unit {unit.sequence}</caption><thead><tr><th scope="col">Cell</th><th scope="col">Type</th><th scope="col">Value</th><th scope="col">Formula</th></tr></thead><tbody>{unit.cells.map(cell => <tr key={cell.column_number}><td>{cell.address}</td><td>{cell.data_type}</td><td>{cell.value ?? 'Not provided'}</td><td><code>{cell.formula ?? '—'}</code></td></tr>)}</tbody></table></div></details>}
        {(unit.native_text || unit.ocr_text) && <details className="source-details"><summary>Original extraction channels</summary>{unit.native_text && <><h4>Native text</h4><pre className="source-text" tabIndex={0}>{unit.native_text}</pre></>}{unit.ocr_text && <><h4>OCR text</h4><pre className="source-text" tabIndex={0}>{unit.ocr_text}</pre></>}</details>}
      </article>)}
      <nav className="pagination" aria-label="Source content pagination"><span>Pages remain bound to this processing attempt.</span><div className="button-group"><button className="button button-small" disabled={!history.length || resource.refreshing} onClick={() => { const previous = history[history.length - 1]; setHistory(current => current.slice(0, -1)); navigate(previous, false); }}>Previous units</button><button className="button button-small" disabled={!page.has_more || resource.refreshing} onClick={() => { if (page.next_after_sequence !== null) navigate(page.next_after_sequence); }}>Next units</button></div></nav>
      <details className="provenance-details"><summary>Source identity</summary><dl><dt>Document</dt><dd>{page.source.original_filename}</dd><dt>Document ID</dt><dd><code>{page.document_id}</code></dd><dt>Extraction ID</dt><dd><code>{page.extraction_id}</code></dd><dt>SHA-256</dt><dd><code>{page.source.sha256}</code></dd></dl></details>
    </>}
  </section>;
}
export function DocumentDetail({ id, api }: { id: string; api: Api }) {
  const [tab, setTab] = useState<'invoice' | 'source'>('invoice');
  const [showOriginal, setShowOriginal] = useState(false);
  const [focus, setFocus] = useState<Focus | null>(null);
  const [revision, setRevision] = useState(0);
  const metadata = useRemote({ key: 'metadata:' + id + ':' + revision, load: signal => api.document(id, signal), poll: data => data?.status === 'queued' || data?.status === 'processing' ? 4000 : 15000 });
  const document = metadata.data;
  const completed = document?.status === 'completed';
  const epoch = document?.processing_finished_at || document?.updated_at || '';
  const invoice = useRemote({ key: JSON.stringify(['invoice', id, epoch, revision]), enabled: completed, load: signal => api.invoice(id, signal) });
  const validation = useRemote({ key: JSON.stringify(['validation', id, invoice.data?.extraction_id, invoice.data?.correction_revision, revision]), enabled: completed && !!invoice.data,
    load: signal => api.validation(id, invoice.data!.extraction_id, signal, invoice.data!.correction_revision ?? 0), poll: () => 15000 });
  const reload = () => { setShowOriginal(false); setFocus(null); setRevision(value => value + 1); };
  const selectEvidence = (reference: Evidence) => { if (!invoice.data) return; setFocus({ reference, extractionId: invoice.data.extraction_id }); setTab('source'); window.requestAnimationFrame(() => documentElementFocus('source-title')); };
  function documentElementFocus(elementId: string) { const element = window.document.getElementById(elementId); element?.setAttribute('tabindex', '-1'); element?.focus(); }
  return <>
    <a className="back-link" href="#/">← Back to document inbox</a>
    <div className="page-heading detail-heading"><div><p className="eyebrow">DOCUMENT WORKSPACE</p><h1>{document?.original_filename || 'Document details'}</h1>{document && <p className="subtitle">{document.extension.slice(1).toUpperCase()} · {formatBytes(document.size_bytes)} · Uploaded {formatDate(document.created_at, true)}</p>}</div><button className="button" onClick={reload} disabled={metadata.refreshing}>Refresh document</button></div>
    {metadata.error && <ErrorPanel error={metadata.error} retry={metadata.refresh} stale={!!document} />}
    {metadata.loading && <Loading label="Loading document" />}
    {document && <><div className="document-statusbar"><Status value={document.status} /><span className="stage-label">{document.processing_stage.replaceAll('_', ' ')}</span><Progress document={document} /></div>
      {(document.status === 'queued' || document.status === 'processing') && <section className="panel processing-panel" role="status"><span className="spinner" aria-hidden="true" /><div><h2>{document.status === 'queued' ? 'Waiting for a worker' : 'Processing this document'}</h2><p>{document.status === 'queued' ? 'Your file is stored and queued. This view updates automatically.' : 'Reading, extraction, and validation must finish before results are published.'}</p>{document.total_units !== null && <small>{document.processed_units} of {document.total_units} source parts read.</small>}{document.error_message && <p className="retry-note">A previous attempt failed. A retry is queued: {document.error_message}</p>}</div></section>}
      {document.status === 'failed' && <section className="panel failed-panel"><p className="eyebrow">PROCESSING FAILED</p><h2>No result was published</h2><p>{document.error_message || 'The worker could not finish processing this document.'}</p>{document.error_code && <code>{document.error_code}</code>}<RetryControl key={document.failed_job_id ?? document.id} api={api} document={document} reload={reload} /><p>You can also submit a replacement source document as a separate upload.</p><a className="button" href="#/?upload=1">Upload the original again</a></section>}
      {completed && <><nav className="detail-tabs" aria-label="Document views"><button className={tab === 'invoice' ? 'selected' : ''} aria-pressed={tab === 'invoice'} onClick={() => setTab('invoice')}>Invoice & validation</button><button className={tab === 'source' ? 'selected' : ''} aria-pressed={tab === 'source'} onClick={() => { setFocus(null); setTab('source'); }}>Source content</button></nav>
        {tab === 'source' ? <SourceViewer key={JSON.stringify([id, invoice.data?.extraction_id, revision, focus?.reference])} id={id} api={api} initialVersion={invoice.data?.extraction_id} focus={focus} reload={reload} /> : <>
          {invoice.loading && <Loading label="Loading invoice results" />}
          {invoice.error && <><ErrorPanel error={invoice.error} retry={invoice.error instanceof ApiError && invoice.error.status === 409 ? reload : invoice.refresh} title="Invoice result is unavailable" /><button className="text-button" onClick={() => setTab('source')}>View extracted source content ↗</button></>}
          {invoice.data && <>
            <CorrectionControls key={invoice.data.extraction_id + ':' + (invoice.data.correction_revision ?? 0)} api={api} response={invoice.data} reload={reload} />
            {invoice.data.original_result && <section className="original-extraction-toggle"><button className="button" aria-expanded={showOriginal} onClick={() => setShowOriginal(current => !current)}>{showOriginal ? 'Hide original extraction' : 'Show original extraction'}</button>{showOriginal && <div className="original-extraction"><h2>Original extraction · preserved without human changes</h2><InvoiceOverview idPrefix="original-" response={{ ...invoice.data, result: invoice.data.original_result, correction_revision: 0 }} onEvidence={selectEvidence} /></div>}</section>}
            <div className="review-layout"><div className="review-main"><InvoiceOverview key={invoice.data.extraction_id + ':' + (invoice.data.correction_revision ?? 0)} response={invoice.data} onEvidence={selectEvidence} /></div><aside className="review-aside" aria-label="Validation results">{validation.loading && <Loading label="Loading validation" />}{validation.error && <ErrorPanel error={validation.error} retry={validation.error instanceof ApiError && validation.error.status === 409 ? reload : validation.refresh} stale={!!validation.data} />}{validation.data && !validation.error && <ValidationPanel data={validation.data} />}</aside></div>
          </>}
        </>}
      </>}
    </>}
  </>;
}
