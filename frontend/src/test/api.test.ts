import { describe, expect, it, vi } from 'vitest';
import { ApiError, createApi, normalizeBase, readLimits } from '../api';
import { formatAmount, formatDate, validateFiles } from '../components';
import { documentHref, parseRoute } from '../hooks';
import { EXTRACTION, ID, OTHER_ID, contentFixture, documentFixture, limitsFixture, listFixture } from './fixtures';

function respond(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
function fetchMock() { const mock = vi.fn<typeof fetch>(); vi.stubGlobal('fetch', mock); return mock; }

describe('HTTP contract', () => {
  it('encodes search filters and uses the configured origin', async () => {
    const mock = fetchMock().mockResolvedValue(respond(listFixture()));
    await createApi('https://backend.example.test').documents({ q: 'A&B 100%', status: 'completed', extension: '.pdf', offset: 20, limit: 20 });
    const url = new URL(String(mock.mock.calls[0][0]));
    expect(url.origin).toBe('https://backend.example.test');
    expect(url.pathname).toBe('/api/documents');
    expect(url.searchParams.get('q')).toBe('A&B 100%');
    expect(url.searchParams.get('status')).toBe('completed');
    expect(url.searchParams.get('offset')).toBe('20');
  });
  it('sends repeated files fields without setting multipart Content-Type', async () => {
    const mock = fetchMock().mockResolvedValue(respond({ count: 2, documents: [documentFixture(), documentFixture({ id: OTHER_ID })] }, 202));
    await createApi('/').upload([new File(['pdf'], 'one.pdf'), new File(['csv'], 'two.csv')]);
    const init = mock.mock.calls[0][1]!;
    expect((init.body as FormData).getAll('files').map(item => (item as File).name)).toEqual(['one.pdf', 'two.csv']);
    expect(new Headers(init.headers).has('Content-Type')).toBe(false);
    expect(init.credentials).toBe('omit');
  });
  it('keeps structured errors and never retries a POST automatically', async () => {
    const mock = fetchMock().mockResolvedValue(respond({ error: { code: 'file_too_large', message: 'Upload exceeds the configured limit.', details: {} } }, 413));
    await expect(createApi('/').upload([new File(['x'], 'large.pdf')])).rejects.toMatchObject({ status: 413, code: 'file_too_large' });
    expect(mock).toHaveBeenCalledTimes(1);
  });
  it('rejects an invalid API shape rather than displaying invented defaults', async () => {
    fetchMock().mockResolvedValue(respond({ items: [], total: 'many' }));
    await expect(createApi('/').documents({ offset: 0, limit: 20 })).rejects.toMatchObject({ code: 'invalid_response' });
  });
  it('binds content cursors to the document and extraction', async () => {
    const mock = fetchMock().mockResolvedValue(respond(contentFixture(2)));
    await createApi('/').content(ID, { after_sequence: 2, limit: 5, extraction_id: EXTRACTION });
    const url = String(mock.mock.calls[0][0]);
    expect(url).toContain('after_sequence=2'); expect(url).toContain('extraction_id=' + EXTRACTION);
    mock.mockResolvedValueOnce(respond({ ...contentFixture(2), extraction_id: OTHER_ID }));
    await expect(createApi('/').content(ID, { after_sequence: 2, limit: 5, extraction_id: EXTRACTION })).rejects.toMatchObject({ code: 'extraction_changed' });
  });
  it('reports network failures without exposing the raw exception', async () => {
    fetchMock().mockRejectedValue(new Error('Internal transport details'));
    await expect(createApi('/').document(ID)).rejects.toMatchObject({ code: 'network_error', status: 0 });
  });
  it('propagates explicit cancellation as AbortError', async () => {
    fetchMock().mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    const controller = new AbortController();
    const promise = createApi('/').document(ID, controller.signal);
    const assertion = expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort(); await assertion;
  });
  it('times out a stalled request', async () => {
    vi.useFakeTimers();
    fetchMock().mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    const assertion = expect(createApi('/').document(ID)).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(30000); await assertion;
  });
});

describe('configuration and exact formatting', () => {
  it('requires explicit public API configuration', () => {
    expect(() => normalizeBase(undefined)).toThrow();
    expect(() => normalizeBase('https://name:secret@example.test')).toThrow();
    expect(() => normalizeBase('javascript:alert(1)')).toThrow();
    expect(() => normalizeBase('https://backend.example.test/api')).toThrow();
    expect(normalizeBase('/')).toBe('');
  });
  it('validates upload limits', () => {
    expect(readLimits({})).toEqual(limitsFixture);
    expect(() => readLimits({ VITE_MAX_UPLOAD_FILES: '-1' })).toThrow();
  });
  it('preserves large decimal values and distinguishes missing from zero', () => {
    expect(formatAmount('9999999999999999.123456', 'USD')).toBe('USD 9,999,999,999,999,999.123456');
    expect(formatAmount(null, 'USD')).toBe('Not provided');
    expect(formatAmount('0', 'USD')).toBe('USD 0.00');
    expect(formatAmount('2.500000', null, false)).toBe('2.500000');
    expect(formatDate('2026-10-30')).toContain('30');
  });
  it('validates file count, type, size and empty files', () => {
    const files = [new File(['hello'], 'invoice.pdf'), new File([], 'empty.csv'), new File(['x'], 'program.exe')];
    const errors = validateFiles(files, { count: 1, fileBytes: 3, totalBytes: 4 });
    expect(errors.some(error => error.includes('at most 1'))).toBe(true);
    expect(errors.some(error => error.includes('combined'))).toBe(true);
    expect(errors.some(error => error.includes('empty'))).toBe(true);
    expect(errors.some(error => error.includes('unsupported'))).toBe(true);
    expect(validateFiles([new File(['x'], 'LEGACY.XLS')], limitsFixture)).toEqual([]);
  });
  it('only recognizes supported document navigation paths', () => {
    expect(parseRoute(documentHref(ID))).toEqual({ kind: 'document', id: ID });
    expect(parseRoute('#/documents/not-a-uuid')).toEqual({ kind: 'missing' });
    expect(parseRoute('#/?upload=1')).toEqual({ kind: 'list' });
    expect(new ApiError('Example', 409).status).toBe(409);
  });
});
