import { useRef, useState, type FormEvent } from 'react';
import { ApiError, type Api } from './api';
import type { CorrectionChanges, DocumentRecord, InvoiceResponse } from './contracts';
import { ErrorPanel, Loading, Pagination, fieldLabel, formatDate } from './components';
import { useRemote } from './hooks';
import './review.css';

const headerFields = ['invoice_number', 'invoice_date', 'supplier_name', 'customer_name', 'currency', 'subtotal', 'tax', 'total'] as const;
const moneyFields = new Set<string>(['subtotal', 'tax', 'total']);
const lineFields = ['description', 'quantity', 'unit_price', 'line_total'] as const;
const exactDecimal = /^-?[0-9]{1,16}(\.[0-9]{1,6})?$/;
const nullable = (value: string) => value.trim() || null;

function freshRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID !== 'function') throw new Error('This action requires HTTPS or localhost in a supported browser.');
  return globalThis.crypto.randomUUID();
}

function Editor({ api, response, saved, reload, cancel }: {
  api: Api; response: InvoiceResponse; saved: () => void; reload: () => void; cancel: () => void;
}) {
  const original = response.result.invoice;
  const [fields, setFields] = useState(() => Object.fromEntries(headerFields.map(key => [key, original[key] ?? ''])) as Record<typeof headerFields[number], string>);
  const [lines, setLines] = useState(() => original.line_items.map(line => ({ ...line })));
  const [complete, setComplete] = useState(original.line_items_complete);
  const [lineOffset, setLineOffset] = useState(0);
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error>();
  const [conflict, setConflict] = useState(false);
  const sending = useRef(false);
  const request = useRef<{ signature: string; id: string } | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (sending.current || conflict) return;
    setError(undefined);
    try {
      const changes: CorrectionChanges = {};
      for (const key of headerFields) {
        const value = nullable(fields[key]);
        if (value !== null && moneyFields.has(key) && !exactDecimal.test(value)) throw new Error(fieldLabel(key) + ' must be an exact decimal without commas or currency symbols.');
        if (key === 'currency' && value !== null && !/^[A-Z]{3}$/.test(value)) throw new Error('Currency must be a three-letter uppercase code.');
        if (value !== original[key]) changes[key] = value;
      }
      const normalized = lines.map((line, index) => {
        const row = { ...line };
        for (const key of lineFields) {
          row[key] = nullable(line[key] ?? '');
          if (key !== 'description' && row[key] !== null && !exactDecimal.test(row[key]!)) throw new Error('Line ' + (index + 1) + ': ' + fieldLabel(key) + ' must be an exact decimal.');
        }
        return row;
      });
      if (JSON.stringify(normalized) !== JSON.stringify(original.line_items)) {
        changes.line_items = normalized; changes.line_items_complete = complete;
      } else if (complete !== original.line_items_complete) changes.line_items_complete = complete;
      if (!Object.keys(changes).length) throw new Error('Change at least one invoice value before saving.');
      if (!reviewer.trim() || !reason.trim()) throw new Error('Enter a reviewer label and a reason for the correction.');
      const command = { extraction_id: response.extraction_id, expected_revision: response.correction_revision ?? 0, reviewer_label: reviewer.trim(), reason: reason.trim(), changes };
      const signature = JSON.stringify(command);
      if (request.current?.signature !== signature) request.current = { signature, id: freshRequestId() };
      sending.current = true; setPending(true);
      await api.correctInvoice(response.document_id, { ...command, request_id: request.current!.id });
      saved();
    } catch (cause) {
      const failure = cause instanceof Error ? cause : new Error('The correction could not be saved.');
      setError(failure);
      if (failure instanceof ApiError && failure.status === 409) setConflict(true);
    } finally { sending.current = false; setPending(false); }
  }

  function updateLine(index: number, key: typeof lineFields[number], value: string) {
    setLines(current => current.map((line, position) => position === index ? { ...line, [key]: nullable(value) } : line));
  }

  return <form className="correction-form" onSubmit={submit}>
    <p className="inline-note">Changes create revision {(response.correction_revision ?? 0) + 1}. Original extraction and source evidence remain available. Blank fields are saved as missing; totals are never automatically adjusted.</p>
    <fieldset disabled={pending || conflict}><legend className="sr-only">Invoice corrections</legend>
      <div className="correction-grid">{headerFields.map(key => <label key={key} htmlFor={'correct-' + key}><span>{fieldLabel(key)}</span><input id={'correct-' + key} type={key === 'invoice_date' ? 'date' : 'text'} inputMode={moneyFields.has(key) ? 'decimal' : undefined} maxLength={key === 'currency' ? 3 : 500} value={fields[key]} onChange={event => setFields(current => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
      <div className="section-heading correction-line-heading"><h3>Correct line items</h3><button className="button button-small" type="button" disabled={lines.length >= 5000} onClick={() => { setLineOffset(Math.floor(lines.length / 10) * 10); setLines(current => [...current, { description: null, quantity: null, unit_price: null, line_total: null }]); }}>Add line item</button></div>
      <div className="table-scroll"><table className="correction-lines"><caption className="sr-only">Editable invoice line items</caption><thead><tr>{lineFields.map(key => <th scope="col" key={key}>{fieldLabel(key)}</th>)}<th scope="col">Action</th></tr></thead><tbody>
        {lines.slice(lineOffset, lineOffset + 10).map((line, index) => { const absolute = lineOffset + index; return <tr key={absolute}>{lineFields.map(key => <td key={key}><input aria-label={'Line ' + (absolute + 1) + ' ' + fieldLabel(key).toLowerCase()} maxLength={key === 'description' ? 500 : 24} inputMode={key === 'description' ? undefined : 'decimal'} value={line[key] ?? ''} onChange={event => updateLine(absolute, key, event.target.value)} /></td>)}<td><button type="button" className="text-button" aria-label={'Remove line ' + (absolute + 1)} onClick={() => { setLines(current => current.filter((_, row) => row !== absolute)); setLineOffset(Math.max(0, Math.min(lineOffset, Math.floor(Math.max(0, lines.length - 2) / 10) * 10))); }}>Remove</button></td></tr>; })}
      </tbody></table></div>
      {lines.length === 0 && <p className="inline-note">No line items. Add a row only if supported by your review.</p>}
      {lines.length > 10 && <nav className="pagination" aria-label="Editable line pagination"><span>{lineOffset + 1}–{Math.min(lineOffset + 10, lines.length)} of {lines.length}</span><div className="button-group"><button type="button" className="button button-small" disabled={lineOffset === 0} onClick={() => setLineOffset(value => Math.max(0, value - 10))}>Previous lines</button><button type="button" className="button button-small" disabled={lineOffset + 10 >= lines.length} onClick={() => setLineOffset(value => value + 10)}>Next lines</button></div></nav>}
      <label className="correction-checkbox"><input type="checkbox" checked={complete} onChange={event => setComplete(event.target.checked)} />I checked that this line-item list is complete.</label>
      <div className="correction-grid"><label htmlFor="reviewer-label"><span>Reviewer label (self-reported)</span><input id="reviewer-label" required maxLength={100} value={reviewer} onChange={event => setReviewer(event.target.value)} /></label><label htmlFor="correction-reason"><span>Reason for correction</span><textarea id="correction-reason" required maxLength={1000} rows={3} value={reason} onChange={event => setReason(event.target.value)} /></label></div>
    </fieldset>
    {error && <div className="notice notice-warning" role="alert"><p>{error.message}</p>{error instanceof ApiError && (error.status === 0 || error.status >= 500) && <p>The server may have saved this request. Saving the same unchanged form again reuses its request ID safely.</p>}</div>}
    {conflict && <div className="notice notice-warning"><p>Your draft has been kept. Copy any needed changes before reloading the newer server revision.</p><button className="button" type="button" onClick={reload}>Reload latest invoice</button></div>}
    <div className="correction-actions"><button type="submit" className="button button-primary" disabled={pending || conflict}>{pending ? 'Saving correction…' : 'Save correction'}</button><button type="button" className="button" disabled={pending} onClick={cancel}>Cancel editing</button></div>
  </form>;
}

function HistoryDetail({ api, id, extractionId, revision }: { api: Api; id: string; extractionId: string; revision: number }) {
  const detail = useRemote({ key: JSON.stringify(['correction', id, extractionId, revision]), load: signal => api.correction(id, extractionId, revision, signal) });
  return <div className="correction-diff">{detail.loading && <Loading label="Loading correction changes" />}{detail.error && <ErrorPanel error={detail.error} retry={detail.refresh} />}{detail.data && <><p>Validation tolerance at save: {detail.data.tolerance}. {detail.data.issues_at_save.length} issue(s) recorded; current duplicate checks may differ.</p><div className="table-scroll"><table><caption>Changes in revision {revision}</caption><thead><tr><th scope="col">Field</th><th scope="col">Before</th><th scope="col">After</th></tr></thead><tbody>{detail.data.changed_fields.map(field => <tr key={field}><th scope="row">{fieldLabel(field)}</th><td><pre>{JSON.stringify(detail.data!.before_values[field], null, 2)}</pre></td><td><pre>{JSON.stringify(detail.data!.after_values[field], null, 2)}</pre></td></tr>)}</tbody></table></div></>}</div>;
}

function History({ api, response }: { api: Api; response: InvoiceResponse }) {
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<number | null>(null);
  const history = useRemote({ key: JSON.stringify(['corrections', response.document_id, response.extraction_id, response.correction_revision, offset]), load: signal => api.corrections(response.document_id, response.extraction_id, offset, signal) });
  return <section className="correction-history" aria-label="Correction history"><h3>Correction history</h3><p>Reviewer labels are self-reported, not authenticated identities.</p>{history.loading && <Loading label="Loading correction history" />}{history.error && <ErrorPanel error={history.error} retry={history.refresh} />}{history.data && <><ol>{history.data.items.map(item => <li key={item.id}><div className="history-heading"><strong>Revision {item.revision} · {item.reviewer_label}</strong><time dateTime={item.created_at}>{formatDate(item.created_at, true)}</time></div><p>{item.reason}</p><small>{item.changed_fields.map(fieldLabel).join(', ')}</small><button type="button" className="text-button" aria-expanded={expanded === item.revision} onClick={() => setExpanded(current => current === item.revision ? null : item.revision)}>{expanded === item.revision ? 'Hide changes' : 'View changes'} for revision {item.revision}</button>{expanded === item.revision && <HistoryDetail api={api} id={response.document_id} extractionId={response.extraction_id} revision={item.revision} />}</li>)}</ol>{history.data.total > 5 && <Pagination offset={offset} count={history.data.items.length} total={history.data.total} limit={5} hasMore={history.data.has_more} onOffset={next => { setExpanded(null); setOffset(next); }} />}</>}</section>;
}

export function CorrectionControls({ api, response, reload }: { api: Api; response: InvoiceResponse; reload: () => void }) {
  const [editing, setEditing] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const revision = response.correction_revision ?? 0;
  return <section className="panel correction-panel" aria-labelledby="human-review-title"><div className="section-heading"><div><p className="eyebrow">HUMAN REVIEW</p><h2 id="human-review-title">Review & corrections</h2><p>{revision ? 'Showing corrected values · revision ' + revision : 'Showing original extracted values'}</p></div>{!editing && <button className="button" onClick={() => setEditing(true)}>Correct invoice values</button>}</div>
    {revision > 0 && <p className="correction-note">Human changes: {(response.corrected_fields ?? []).map(fieldLabel).join(', ')}. Automated checks are recalculated; a correction does not constitute formal approval.</p>}
    {editing && <Editor api={api} response={response} cancel={() => setEditing(false)} saved={() => { setEditing(false); reload(); }} reload={reload} />}
    {revision > 0 && <div className="correction-history-toggle"><button className="text-button" aria-expanded={historyOpen} onClick={() => setHistoryOpen(current => !current)}>{historyOpen ? 'Hide correction history' : 'Show correction history'}</button>{historyOpen && <History api={api} response={response} />}</div>}
  </section>;
}

export function RetryControl({ api, document, reload }: { api: Api; document: DocumentRecord; reload: () => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error>();
  const [blocked, setBlocked] = useState(false);
  const requestId = useRef<string | undefined>(undefined);
  const sending = useRef(false);
  if (document.status !== 'failed' || !document.failed_job_id) return null;
  async function retry() {
    if (sending.current || blocked || !document.failed_job_id) return;
    setError(undefined);
    try {
      requestId.current ??= freshRequestId();
      sending.current = true; setPending(true);
      await api.reprocess(document.id, { request_id: requestId.current, failed_job_id: document.failed_job_id });
      reload();
    } catch (cause) {
      const failure = cause instanceof Error ? cause : new Error('The retry could not be queued.');
      setError(failure);
      if (failure instanceof ApiError && failure.status === 409) setBlocked(true);
    } finally { sending.current = false; setPending(false); }
  }
  return <div className="retry-controls"><p>After resolving the error, queue the stored original for a new processing job. Previous failures remain in processing history.</p><button type="button" className="button button-primary" disabled={pending || blocked} onClick={() => void retry()}>{pending ? 'Queueing retry…' : 'Retry failed job'}</button>{error && <div role="alert" className="notice notice-warning"><p>{error.message}</p>{!blocked && <p>A retry may already have been accepted. This button reuses the same request ID.</p>}</div>}{blocked && <button type="button" className="button" onClick={reload}>Refresh job status</button>}</div>;
}
