import { useEffect, useRef, useState } from 'react';
import type { Api, ListQuery, UploadLimits } from './api';
import { extensions, statuses, type DocumentStatus } from './contracts';
import { documentHref, parseRoute, useLocationHash, useRemote } from './hooks';
import { EmptyState, ErrorPanel, Loading, Pagination, Progress, Status, UploadPanel, formatBytes, formatDate, statusLabels } from './components';
import { DocumentDetail } from './DocumentDetail';

export function App({ api, limits, onSignOut }: { api: Api; limits: UploadLimits; onSignOut?: () => void }) {
  const hash = useLocationHash();
  const route = parseRoute(hash);
  const main = useRef<HTMLElement>(null);
  const [query, setQuery] = useState<ListQuery>({ q: '', status: '', extension: '', offset: 0, limit: 20 });
  const [searchDraft, setSearchDraft] = useState('');
  const [notice, setNotice] = useState('');
  const [revision, setRevision] = useState(0);
  const listing = useRemote({
    key: JSON.stringify(['documents', query, revision]), enabled: route.kind === 'list',
    load: signal => api.documents(query, signal),
    poll: data => data?.items.some(item => item.status === 'queued' || item.status === 'processing') ? 4000 : 15000,
  });
  useEffect(() => { main.current?.focus(); }, [hash]);
  useEffect(() => {
    if (route.kind === 'list' && hash.includes('upload=1')) document.getElementById('choose-files')?.focus();
  }, [hash, route.kind]);
  useEffect(() => {
    if (listing.data && query.offset > 0 && listing.data.items.length === 0) setQuery(current => ({ ...current, offset: Math.max(0, Math.ceil(listing.data!.total / current.limit) - 1) * current.limit }));
  }, [listing.data, query.offset]);
  function chooseFiles() {
    if (route.kind !== 'list') { window.location.hash = '#/?upload=1'; return; }
    document.getElementById('upload-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    document.getElementById('choose-files')?.focus();
  }
  return <div className="app-shell">
    <a href="#main-content" className="skip-link" onClick={event => { event.preventDefault(); main.current?.focus(); }}>Skip to main content</a>
    <aside className="sidebar" aria-label="Workspace navigation"><a className="brand" href="#/" aria-label="Document Intelligence home"><span className="brand-mark" aria-hidden="true">di<span>·</span></span><span>Document<br />Intelligence</span></a>
      <p className="nav-caption">WORKSPACE</p><nav><a className="nav-link active" href="#/" aria-current={route.kind === 'list' ? 'page' : undefined}><span aria-hidden="true">▤</span>Document inbox</a>
        <button className="nav-link" onClick={chooseFiles}><span aria-hidden="true">＋</span>Upload documents</button></nav>
      <div className="sidebar-note"><span className="small-rule" /><strong>Evidence comes first.</strong><p>Inspect source references and resolve exceptions before relying on extracted values.</p></div>
      <div className="sidebar-footer">Financial document operations</div>
    </aside>
    <div className="workspace"><header className="topbar"><span>Operations console</span>{onSignOut ? <button className="button button-small access-lock" onClick={onSignOut}>Lock workspace</button> : <span className="topbar-label">DOCUMENT REVIEW</span>}</header>
      <main id="main-content" tabIndex={-1} ref={main}>
        {notice && <div className="notice notice-success" role="status"><span>{notice}</span><button className="icon-button" aria-label="Dismiss upload notification" onClick={() => setNotice('')}>×</button></div>}
        {route.kind === 'document' ? <DocumentDetail key={route.id} id={route.id} api={api} /> : route.kind === 'missing' ? <EmptyState title="Page not found"><a className="button" href="#/">Return to documents</a></EmptyState> : <>
          <div className="page-heading"><div><p className="eyebrow">DOCUMENT OPERATIONS</p><h1>Document inbox</h1><p className="subtitle">Review invoices, processing status, and source evidence.</p></div><div className="button-group"><button className="button" disabled={listing.refreshing} onClick={listing.refresh}>{listing.refreshing ? 'Refreshing…' : 'Refresh'}</button><button className="button button-primary" onClick={chooseFiles}>Add documents</button></div></div>
          <UploadPanel api={api} limits={limits} onAccepted={count => { setNotice(count + (count === 1 ? ' document accepted. ' : ' documents accepted. ') + 'The worker will process the queued files.'); setQuery(current => ({ ...current, q: '', status: '', extension: '', offset: 0 })); setSearchDraft(''); setRevision(value => value + 1); }} />
          <section className="panel inbox" aria-labelledby="documents-title"><div className="section-heading"><div><h2 id="documents-title">Documents {listing.data && <span className="count">{listing.data.total}</span>}</h2><p>Search filenames, extracted text, suppliers, and invoice numbers.</p></div>{listing.updatedAt && <span className="sync-label">Updated {listing.updatedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>}</div>
            <form className="filters" onSubmit={event => { event.preventDefault(); setQuery(current => ({ ...current, q: searchDraft.trim(), offset: 0 })); }}>
              <div className="search-control"><label className="sr-only" htmlFor="document-search">Search documents</label><input id="document-search" type="search" maxLength={200} placeholder="Search documents or invoice details" value={searchDraft} onChange={event => setSearchDraft(event.target.value)} /><button className="button" type="submit">Search</button></div>
              <label className="filter-control"><span>Status</span><select value={query.status} onChange={event => setQuery(current => ({ ...current, status: event.target.value as DocumentStatus | '', offset: 0 }))}><option value="">All statuses</option>{statuses.map(status => <option key={status} value={status}>{statusLabels[status]}</option>)}</select></label>
              <label className="filter-control"><span>Format</span><select value={query.extension} onChange={event => setQuery(current => ({ ...current, extension: event.target.value, offset: 0 }))}><option value="">All formats</option>{extensions.map(extension => <option key={extension} value={extension}>{extension.slice(1).toUpperCase()}</option>)}</select></label>
              {(query.q || query.status || query.extension) && <button type="button" className="text-button" onClick={() => { setQuery({ q: '', status: '', extension: '', offset: 0, limit: 20 }); setSearchDraft(''); }}>Clear filters</button>}
            </form>
            {listing.error && <ErrorPanel error={listing.error} retry={listing.refresh} stale={!!listing.data} />}
            {listing.loading && <Loading />}
            {listing.data && (listing.data.items.length ? <><div className="table-scroll"><table className="documents-table"><caption className="sr-only">Uploaded financial documents</caption><thead><tr><th scope="col">Document</th><th scope="col">Format</th><th scope="col">Status</th><th scope="col">Progress</th><th scope="col">Uploaded</th><th scope="col"><span className="sr-only">Open document</span></th></tr></thead><tbody>{listing.data.items.map(item => <tr key={item.id}><td><a className="file-link" href={documentHref(item.id)}><span className="file-symbol" aria-hidden="true">{item.extension.slice(1).toUpperCase()}</span><span className="file-copy"><strong>{item.original_filename}</strong><small>{formatBytes(item.size_bytes)}{item.status === 'failed' && item.error_message ? ' · ' + item.error_message : ''}</small></span></a></td><td className="format-column">{item.extension.slice(1).toUpperCase()}</td><td><Status value={item.status} /></td><td><Progress document={item} /></td><td><time dateTime={item.created_at}>{formatDate(item.created_at, true)}</time></td><td><a className="open-link" aria-label={'Open ' + item.original_filename} href={documentHref(item.id)}>Open <span aria-hidden="true">↗</span></a></td></tr>)}</tbody></table></div><Pagination offset={query.offset} count={listing.data.items.length} total={listing.data.total} hasMore={listing.data.has_more} limit={query.limit} busy={listing.refreshing} onOffset={offset => setQuery(current => ({ ...current, offset }))} /></> : <EmptyState title={query.q || query.status || query.extension ? 'No matching documents' : 'Your document inbox is ready'}><p>{query.q || query.status || query.extension ? 'Try another search or clear the filters.' : 'Upload an invoice or financial document to begin processing.'}</p></EmptyState>)}
          </section><p className="page-footnote">Queued and processing files update automatically. Completion means processing finished; review the validation results separately.</p>
        </>}
      </main></div>
  </div>;
}
