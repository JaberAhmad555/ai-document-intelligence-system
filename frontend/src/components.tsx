import { useRef, useState, type ReactNode } from 'react';
import type { Api, UploadLimits } from './api';
import { ApiError } from './api';
import { extensions, type DocumentRecord, type DocumentStatus, type Position } from './contracts';

export const statusLabels: Record<DocumentStatus, string> = { queued: 'Queued', processing: 'Processing', completed: 'Completed', failed: 'Failed' };
export function Status({ value }: { value: DocumentStatus }) {
  return <span className={'status status-' + value}><span className="status-dot" aria-hidden="true" />{statusLabels[value]}</span>;
}
export function Progress({ document }: { document: DocumentRecord }) {
  return <div className="progress-cell"><progress max={100} value={document.progress_percent} aria-label={'Processing progress for ' + document.original_filename} />
    <span>{document.progress_percent}%</span></div>;
}
export function formatAmount(value: string | null, currency?: string | null, monetary = true): string {
  if (value === null) return 'Not provided';
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) return value;
  const integer = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = monetary ? (match[3] || '').padEnd(2, '0') : match[3];
  return (currency ? currency + ' ' : '') + match[1] + integer + (fraction ? '.' + fraction : '');
}
export function formatDate(value: string | null, time = false): string {
  if (!value) return 'Not provided';
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? value + 'T00:00:00' : value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('en', { dateStyle: 'medium', ...(time ? { timeStyle: 'short' as const } : {}) }).format(date);
}
export function formatBytes(value: number) { return value < 1024 ? value + ' B' : value < 1024 * 1024 ? (value / 1024).toFixed(1) + ' KiB' : (value / (1024 * 1024)).toFixed(1) + ' MiB'; }
export function fieldLabel(path: string) { return path.replace(/^line_items\.(\d+)\./, (_, index: string) => 'Line ' + (Number(index) + 1) + ' · ').replaceAll('_', ' '); }
export function positionLabel(position: Position): string {
  const parts: string[] = [];
  if (position.page_number) parts.push('Page ' + position.page_number);
  if (position.frame_number) parts.push('Frame ' + position.frame_number);
  if (position.sheet_name) parts.push('Sheet “' + position.sheet_name + '”');
  if (position.row_number) parts.push('Row ' + position.row_number);
  if (position.physical_line_start) parts.push('CSV line ' + position.physical_line_start + (position.physical_line_end !== position.physical_line_start ? '–' + position.physical_line_end : ''));
  return parts.join(' · ') || 'Part ' + position.part_number + ' of ' + position.part_count;
}
export function ErrorPanel({ error, retry, title = 'Request could not be completed', stale = false }: { error: Error; retry?: () => void; title?: string; stale?: boolean }) {
  return <div className="notice notice-error" role="alert"><div><strong>{title}</strong><p>{error.message}</p>
    {stale && <p>Showing the last retrieved data.</p>}
    {error instanceof ApiError && error.code && <small className="error-code">{error.code}</small>}</div>
    {retry && <button className="button button-small" onClick={retry}>Retry request</button>}</div>;
}
export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return <div className="empty-state"><span className="empty-mark" aria-hidden="true">▤</span><h3>{title}</h3><div>{children}</div></div>;
}
export function Loading({ label = 'Loading documents' }: { label?: string }) { return <div className="loading-state" role="status"><span className="spinner" aria-hidden="true" />{label}…</div>; }
export function Pagination({ offset, count, total, hasMore, limit, onOffset, busy = false }: {
  offset: number; count: number; total: number; hasMore: boolean; limit: number; onOffset: (value: number) => void; busy?: boolean;
}) {
  return <nav className="pagination" aria-label="Results pagination"><span>{count ? offset + 1 : 0}–{offset + count} of {total}</span>
    <div className="button-group"><button className="button button-small" disabled={offset === 0 || busy} onClick={() => onOffset(Math.max(0, offset - limit))}>Previous</button>
      <button className="button button-small" disabled={!hasMore || busy} onClick={() => onOffset(offset + limit)}>Next</button></div></nav>;
}
function fileKey(file: File) { return JSON.stringify([file.name, file.size, file.lastModified]); }
export function validateFiles(files: File[], limits: UploadLimits): string[] {
  const errors: string[] = [];
  if (files.length > limits.count) errors.push('Select at most ' + limits.count + ' files per upload.');
  if (files.reduce((sum, file) => sum + file.size, 0) > limits.totalBytes) errors.push('The combined upload exceeds ' + formatBytes(limits.totalBytes) + '.');
  for (const file of files) {
    const extension = '.' + (file.name.split('.').pop() || '').toLowerCase();
    if (!(extensions as readonly string[]).includes(extension)) errors.push(file.name + ': unsupported file format.');
    if (file.size === 0) errors.push(file.name + ': the file is empty.');
    if (file.size > limits.fileBytes) errors.push(file.name + ': exceeds ' + formatBytes(limits.fileBytes) + '.');
  }
  return errors;
}
export function UploadPanel({ api, limits, onAccepted }: { api: Api; limits: UploadLimits; onAccepted: (count: number) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<Error>();
  const problems = validateFiles(files, limits);
  const addFiles = (incoming: File[]) => {
    if (sending) return;
    setError(undefined);
    setFiles(current => [...new Map([...current, ...incoming].map(file => [fileKey(file), file])).values()]);
  };
  async function submit() {
    if (!files.length || problems.length || sending) return;
    setSending(true); setError(undefined);
    try { const accepted = await api.upload(files); setFiles([]); onAccepted(accepted.count); }
    catch (cause) { setError(cause instanceof Error ? cause : new Error('Upload failed.')); }
    finally { setSending(false); }
  }
  return <section className="panel upload-panel" aria-labelledby="upload-title" id="upload-panel">
    <div className={'drop-zone' + (dragging ? ' is-dragging' : '')}
      onDragOver={event => { event.preventDefault(); if (!sending) setDragging(true); }}
      onDragLeave={event => { if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setDragging(false); }}
      onDrop={event => { event.preventDefault(); setDragging(false); addFiles(Array.from(event.dataTransfer.files)); }}>
      <div className="upload-symbol" aria-hidden="true">↑</div><div className="drop-copy"><h2 id="upload-title">Upload financial documents</h2>
        <p>Drop files here, or choose them from your device.</p><small>PDF, JPG, PNG, TIFF, XLSX, XLS, CSV · {formatBytes(limits.fileBytes)} per file · up to {limits.count} files</small></div>
      <input ref={input} type="file" hidden multiple accept={extensions.join(',')} aria-label="Select documents" disabled={sending}
        onChange={event => { addFiles(Array.from(event.currentTarget.files || [])); event.currentTarget.value = ''; }} />
      <button id="choose-files" className="button" disabled={sending} onClick={() => input.current?.click()}>Choose files</button>
    </div>
    {files.length > 0 && <div className="upload-selection"><ul aria-label="Selected files">{files.map(file => <li key={fileKey(file)}><span className="truncate" title={file.name}>{file.name}</span><small>{formatBytes(file.size)}</small>
      <button className="icon-button" disabled={sending} aria-label={'Remove ' + file.name} onClick={() => setFiles(current => current.filter(item => fileKey(item) !== fileKey(file)))}>×</button></li>)}</ul>
      <div className="upload-footer"><span>{files.length} selected · {formatBytes(files.reduce((sum, file) => sum + file.size, 0))}</span>
        <button className="button button-primary" disabled={sending || problems.length > 0} onClick={() => void submit()}>{sending ? 'Sending files…' : 'Upload ' + files.length + (files.length === 1 ? ' document' : ' documents')}</button></div>
    </div>}
    {problems.length > 0 && <div className="notice notice-warning" role="alert"><ul>{problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul></div>}
    {sending && <div className="upload-progress" role="status"><progress aria-label="Sending selected files" /><span>Uploading. Processing starts after the server accepts the files.</span></div>}
    {error && <ErrorPanel title="Upload was not confirmed" error={error} />}
    {error instanceof ApiError && error.status === 0 && <p className="inline-note">The server may have received the files. Refresh the document list before sending them again.</p>}
  </section>;
}
