/** Synthetic credential/transport tests. No live backend or real password. */
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DemoAccess, DEMO_IDLE_TIMEOUT_MS } from '../DemoAccess';
import { checkSecureApiLocation, createDemoSession } from '../demoSession';
import { EXTRACTION, ID, OTHER_ID, contentFixture, documentFixture, invoiceFixture, limitsFixture, listFixture, validationFixture } from './fixtures';

// Synthetic test value; never configure this on a deployed backend.
const TOKEN = 'test-only-' + 'x'.repeat(34);
const REQUEST = '44444444-4444-4444-8444-444444444444';
const receipt = { id: OTHER_ID, request_id: REQUEST, document_id: ID, extraction_id: EXTRACTION, revision: 1, created_at: '2026-10-30T09:00:00Z' };
const correction = { request_id: REQUEST, extraction_id: EXTRACTION, expected_revision: 0, reviewer_label: 'Test reviewer', reason: 'Synthetic review', changes: { total: '110.00' } };
const history = { ...receipt, reviewer_label: 'Test reviewer', reason: 'Synthetic review', changed_fields: ['total'], original_digest: 'a'.repeat(64), tolerance: '0.02' };

function respond(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
function deny() { return respond({ error: { code: 'authentication_required', message: 'Access required.' } }, 401); }
function mockServer() {
  const fetcher = vi.fn<typeof fetch>().mockImplementation(async url => {
    const path = new URL(String(url), window.location.origin).pathname;
    if (path === '/api/auth/check') return respond({ authenticated: true });
    if (path === '/api/documents') return respond(listFixture());
    throw new Error('Unexpected test route');
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}
async function signIn(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Demo password'), TOKEN);
  await user.click(screen.getByRole('button', { name: 'Open workspace' }));
}
beforeEach(() => { window.history.replaceState(null, '', '/#/'); });


describe('protected request transport', () => {
  it('adds authorization to every existing API operation, including mutations and evidence', async () => {
    const fetcher = vi.fn<typeof fetch>(); vi.stubGlobal('fetch', fetcher);
    const session = createDemoSession('/', TOKEN, vi.fn());
    const api = session.api;
    const calls: Array<{ run: () => Promise<unknown>; body: unknown }> = [
      { run: () => api.checkAccess(), body: { authenticated: true } },
      { run: () => api.documents({ q: 'source', limit: 20, offset: 0 }), body: listFixture() },
      { run: () => api.document(ID), body: documentFixture() },
      { run: () => api.upload([new File(['csv'], 'synthetic.csv')]), body: { count: 1, documents: [documentFixture()] } },
      { run: () => api.invoice(ID), body: invoiceFixture() },
      { run: () => api.validation(ID, EXTRACTION), body: validationFixture() },
      { run: () => api.content(ID, { after_sequence: 0, limit: 5, extraction_id: EXTRACTION }), body: contentFixture() },
      { run: () => api.correctInvoice(ID, correction), body: receipt },
      { run: () => api.corrections(ID, EXTRACTION, 0), body: { document_id: ID, extraction_id: EXTRACTION, items: [history], total: 1, offset: 0, limit: 5, has_more: false } },
      { run: () => api.correction(ID, EXTRACTION, 1), body: { ...history, corrected_fields: ['total'], before_values: { total: '120.00' }, after_values: { total: '110.00' }, issues_at_save: [] } },
      { run: () => api.reprocess(ID, { request_id: REQUEST, failed_job_id: OTHER_ID }), body: { request_id: REQUEST, document_id: ID, failed_job_id: OTHER_ID, new_job_id: EXTRACTION, created_at: receipt.created_at } },
    ];
    for (const item of calls) { fetcher.mockResolvedValueOnce(respond(item.body)); await item.run(); }
    expect(fetcher).toHaveBeenCalledTimes(calls.length);
    for (const [url, init] of fetcher.mock.calls) {
      expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer ' + TOKEN);
      expect(String(url)).not.toContain(TOKEN);
      expect(init).toMatchObject({ cache: 'no-store', credentials: 'omit', redirect: 'error' });
      if (typeof init?.body === 'string') expect(init.body).not.toContain(TOKEN);
      if (init?.body instanceof FormData) {
        expect(new Headers(init.headers).has('Content-Type')).toBe(false);
        expect(init.body.get('token')).toBeNull();
      }
    }
    session.dispose();
  });

  it('invalidates access on 401 without echoing a server body or resending a POST', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(respond({ error: { code: 'bad', message: TOKEN } }, 401));
    vi.stubGlobal('fetch', fetcher);
    const unauthorized = vi.fn(); const session = createDemoSession('/', TOKEN, unauthorized);
    await expect(session.api.reprocess(ID, { request_id: REQUEST, failed_job_id: OTHER_ID })).rejects.toMatchObject({ status: 401, code: 'authentication_required', message: expect.not.stringContaining(TOKEN) });
    expect(unauthorized).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(1);
    session.dispose();
  });

  it('aborts outstanding requests and refuses further requests after disposal', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    vi.stubGlobal('fetch', fetcher);
    const session = createDemoSession('/', TOKEN, vi.fn());
    const waiting = session.api.document(ID);
    const rejected = expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
    session.dispose(); await rejected;
    await expect(session.api.upload([new File(['csv'], 'synthetic.csv')])).rejects.toMatchObject({ status: 401 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('discards a late 401 from a disposed session without invalidating a new one', async () => {
    let resolveOld!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; })).mockResolvedValue(respond({ authenticated: true }));
    vi.stubGlobal('fetch', fetcher);
    const oldUnauthorized = vi.fn(); const newUnauthorized = vi.fn();
    const old = createDemoSession('/', TOKEN, oldUnauthorized);
    const request = old.api.document(ID);
    const rejected = expect(request).rejects.toMatchObject({ name: 'AbortError' });
    old.dispose();
    const next = createDemoSession('/', 'test-only-' + 'y'.repeat(34), newUnauthorized);
    await next.api.checkAccess();
    resolveOld(deny()); await rejected;
    expect(oldUnauthorized).not.toHaveBeenCalled(); expect(newUnauthorized).not.toHaveBeenCalled();
    next.dispose();
  });

  it('refuses insecure remote API origins before sending a password', () => {
    const fetcher = vi.fn<typeof fetch>(); vi.stubGlobal('fetch', fetcher);
    expect(() => createDemoSession('http://public.example.test', TOKEN, vi.fn())).toThrow(/HTTPS/);
    expect(() => checkSecureApiLocation('https://public.example.test')).not.toThrow();
    expect(() => checkSecureApiLocation('http://127.0.0.1:8000')).not.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('does not interpret explicit backend test/development bypass as authenticated access', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(respond({ authenticated: false })));
    const session = createDemoSession('/', TOKEN, vi.fn());
    await expect(session.api.checkAccess()).rejects.toMatchObject({ code: 'demo_access_not_configured' });
    session.dispose();
  });
});


it('does not request documents until access succeeds and does not use browser storage', async () => {
  const fetcher = mockServer(); const user = userEvent.setup();
  const readStorage = vi.spyOn(Storage.prototype, 'getItem');
  const writeStorage = vi.spyOn(Storage.prototype, 'setItem');
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  expect(screen.getByRole('heading', { name: 'Open the review workspace' })).toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
  await signIn(user);
  expect(await screen.findByText('synthetic-review.pdf')).toBeInTheDocument();
  expect(String(fetcher.mock.calls[0][0])).toBe('/api/auth/check');
  expect(readStorage).not.toHaveBeenCalled(); expect(writeStorage).not.toHaveBeenCalled();
  expect(window.location.href).not.toContain(TOKEN);
  await user.click(screen.getByRole('button', { name: 'Lock workspace' }));
  expect(screen.getByLabelText('Demo password')).toHaveValue('');
  expect(screen.queryByText('synthetic-review.pdf')).not.toBeInTheDocument();
});


it('rejects invalid credentials without revealing any document data', async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(deny()); vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user);
  expect(await screen.findByText(/Demo access is no longer valid/)).toBeInTheDocument();
  expect(screen.getByLabelText('Demo password')).toHaveValue('');
  expect(screen.queryByRole('button', { name: 'Lock workspace' })).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
});


it('requires the password again after a page reload/remount', async () => {
  const fetcher = mockServer(); const user = userEvent.setup();
  const first = render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user); await screen.findByText('synthetic-review.pdf');
  first.unmount(); fetcher.mockClear();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  expect(screen.getByLabelText('Demo password')).toHaveValue('');
  expect(screen.queryByText('synthetic-review.pdf')).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});


it('locks and removes cached document views when a protected request receives 401', async () => {
  const fetcher = mockServer(); const user = userEvent.setup();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user); await screen.findByText('synthetic-review.pdf');
  fetcher.mockResolvedValueOnce(deny());
  await user.click(screen.getByRole('button', { name: 'Refresh' }));
  expect(await screen.findByLabelText('Demo password')).toHaveValue('');
  expect(screen.queryByText('synthetic-review.pdf')).not.toBeInTheDocument();
  expect(screen.getByText(/Demo access is no longer valid/)).toBeInTheDocument();
});


it('keeps the session after upload limit errors', async () => {
  const fetcher = mockServer(); const user = userEvent.setup();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user); await screen.findByText('synthetic-review.pdf');
  fetcher.mockResolvedValueOnce(respond({ error: { code: 'file_too_large', message: 'Synthetic upload limit.' } }, 413));
  await user.upload(screen.getByLabelText('Select documents'), new File(['invoice,total\nSYN,10'], 'synthetic.csv', { type: 'text/csv' }));
  await user.click(screen.getByRole('button', { name: 'Upload 1 document' }));
  expect(await screen.findByText('Synthetic upload limit.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Lock workspace' })).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
});


it('does not replay an unauthorized upload after the reviewer signs in again', async () => {
  const fetcher = mockServer(); const user = userEvent.setup();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user); await screen.findByText('synthetic-review.pdf');
  fetcher.mockResolvedValueOnce(deny());
  await user.upload(screen.getByLabelText('Select documents'), new File(['csv'], 'synthetic.csv'));
  await user.click(screen.getByRole('button', { name: 'Upload 1 document' }));
  await screen.findByLabelText('Demo password');
  await signIn(user); await screen.findByText('synthetic-review.pdf');
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  expect(screen.queryByRole('button', { name: 'Upload 1 document' })).not.toBeInTheDocument();
});


it('locks idle sessions on return even if background timers were throttled', async () => {
  mockServer(); const user = userEvent.setup();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user); await screen.findByText('synthetic-review.pdf');
  const expired = Date.now() + DEMO_IDLE_TIMEOUT_MS + 1000;
  vi.spyOn(Date, 'now').mockReturnValue(expired);
  fireEvent.focus(window);
  expect(await screen.findByLabelText('Demo password')).toHaveValue('');
  expect(screen.getByText(/Workspace locked after 30 minutes/)).toBeInTheDocument();
});


it('clears access when leaving the page, including back-forward-cache navigation', async () => {
  mockServer(); const user = userEvent.setup();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user); await screen.findByText('synthetic-review.pdf');
  act(() => { window.dispatchEvent(new Event('pagehide')); });
  expect(screen.getByLabelText('Demo password')).toHaveValue('');
  expect(screen.queryByText('synthetic-review.pdf')).not.toBeInTheDocument();
});


it('handles a temporary verification outage without retaining the entered password', async () => {
  const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error('Synthetic transport detail'));
  vi.stubGlobal('fetch', fetcher); const user = userEvent.setup();
  render(<DemoAccess baseUrl="/" limits={limitsFixture} />);
  await signIn(user);
  expect(await screen.findByRole('alert')).toHaveTextContent(/Cannot reach the API/);
  expect(screen.getByLabelText('Demo password')).toHaveValue('');
  expect(fetcher).toHaveBeenCalledTimes(1);
});
