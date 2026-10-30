import { z } from 'zod';
import {
  correctionReceiptSchema, correctionPageSchema, correctionDetailSchema, reprocessReceiptSchema,
  type CorrectionCommand, type CorrectionReceipt, type CorrectionPage, type CorrectionDetail,
  type ReprocessCommand, type ReprocessReceipt,
  contentSchema, documentListSchema, documentSchema, invoiceResponseSchema, uploadSchema, validationSchema,
  type ContentPage, type DocumentList, type DocumentRecord, type DocumentStatus,
  type InvoiceResponse, type UploadResponse, type ValidationResponse,
} from './contracts';

export class ApiError extends Error {
  constructor(message: string, public status = 0, public code = 'network_error') { super(message); this.name = 'ApiError'; }
}
export interface ListQuery { q?: string; status?: DocumentStatus | ''; extension?: string; offset: number; limit: number }
export interface ContentQuery { after_sequence: number; limit: number; extraction_id?: string }
export interface Api {
  documents(query: ListQuery, signal?: AbortSignal): Promise<DocumentList>;
  document(id: string, signal?: AbortSignal): Promise<DocumentRecord>;
  upload(files: File[], signal?: AbortSignal): Promise<UploadResponse>;
  invoice(id: string, signal?: AbortSignal): Promise<InvoiceResponse>;
  validation(id: string, extractionId: string, signal?: AbortSignal, revision?: number): Promise<ValidationResponse>;
  content(id: string, query: ContentQuery, signal?: AbortSignal): Promise<ContentPage>;
  correctInvoice(id: string, command: CorrectionCommand): Promise<CorrectionReceipt>;
  corrections(id: string, extractionId: string, offset: number, signal?: AbortSignal): Promise<CorrectionPage>;
  correction(id: string, extractionId: string, revision: number, signal?: AbortSignal): Promise<CorrectionDetail>;
  reprocess(id: string, command: ReprocessCommand): Promise<ReprocessReceipt>;
}
export interface UploadLimits { count: number; fileBytes: number; totalBytes: number }
export function readLimits(env: Record<string, unknown>): UploadLimits {
  const positive = (name: string, fallback: number) => {
    const value = env[name] == null || env[name] === '' ? fallback : Number(env[name]);
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(name + ' must be a positive integer.');
    return value;
  };
  return {
    count: positive('VITE_MAX_UPLOAD_FILES', 10),
    fileBytes: positive('VITE_MAX_FILE_SIZE_MB', 25) * 1024 * 1024,
    totalBytes: positive('VITE_MAX_UPLOAD_TOTAL_MB', 100) * 1024 * 1024,
  };
}
export function normalizeBase(value: string | undefined): string {
  const base = value?.trim();
  if (!base) throw new Error('VITE_API_BASE_URL is missing. Copy .env.example to .env and restart the frontend.');
  if (base === '/') return '';
  let url: URL;
  try { url = new URL(base); } catch { throw new Error('VITE_API_BASE_URL must be / or an absolute HTTP(S) backend URL.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('The API URL must use HTTP(S), without credentials, a query, or a fragment.');
  }
  if (/\/api\/?$/.test(url.pathname)) throw new Error('Use the backend base URL without a trailing /api.');
  return url.toString().replace(/\/$/, '');
}
function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== '') params.set(key, String(value));
  return params.toString();
}
const errorSchema = z.object({ error: z.object({ code: z.string(), message: z.string() }) });
export interface ApiAccess {
  getToken?: () => string | null;
  requireAuthentication?: boolean;
  onUnauthorized?: () => void;
  sessionSignal?: AbortSignal;
}
export interface DemoApi extends Api { checkAccess(signal?: AbortSignal): Promise<void> }
export function createApi(baseUrl: string | undefined, access: ApiAccess = {}): DemoApi {
  const base = normalizeBase(baseUrl);
  async function request<T>(path: string, schema: z.ZodType<T>, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted || access.sessionSignal?.aborted) controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    access.sessionSignal?.addEventListener('abort', abort, { once: true });
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, init.method === 'POST' || path === '/api/auth/check' ? 120000 : 30000);
    try {
      const headers = new Headers(init.headers);
      headers.set('Accept', 'application/json');
      const token = access.getToken?.() ?? null;
      if (access.requireAuthentication && !token) throw new ApiError('Enter the demo password to continue.', 401, 'authentication_required');
      if (token !== null) {
        if (!/^[A-Za-z0-9_-]{32,256}$/.test(token)) throw new ApiError('The demo password is not valid.', 401, 'authentication_required');
        headers.set('Authorization', 'Bearer ' + token);
      }
      if (controller.signal.aborted) throw new DOMException('Request cancelled', 'AbortError');
      const response = await fetch(base + path, {
        ...init, signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error', headers,
      });
      if (signal?.aborted || access.sessionSignal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      if (response.status === 401) {
        // Ignore late failures from a credential/session that has been replaced.
        if ((access.getToken?.() ?? null) === token) access.onUnauthorized?.();
        throw new ApiError('Demo access is no longer valid. Enter the current demo password.', 401, 'authentication_required');
      }
      const text = await response.text();
      if (signal?.aborted || access.sessionSignal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      let body: unknown;
      try { body = text ? JSON.parse(text) : null; } catch { body = null; }
      if (!response.ok) {
        const structured = errorSchema.safeParse(body);
        throw new ApiError(
          structured.success ? structured.data.error.message : 'The server returned HTTP ' + response.status + '. Try refreshing the request.',
          response.status, structured.success ? structured.data.error.code : 'http_error',
        );
      }
      const parsed = schema.safeParse(body);
      if (!parsed.success) throw new ApiError('The API returned an unexpected response. Check that the frontend and backend versions match.', response.status, 'invalid_response');
      return parsed.data;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) throw error;
      if (signal?.aborted || access.sessionSignal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      if (error instanceof ApiError) throw error;
      throw new ApiError(timedOut ? 'The API request timed out.' : 'Cannot reach the API. Check the backend connection and CORS configuration.', 0, timedOut ? 'timeout' : 'network_error');
    } finally {
      window.clearTimeout(timeout);
      signal?.removeEventListener('abort', abort);
      access.sessionSignal?.removeEventListener('abort', abort);
    }
  }
  const prefix = (id: string) => '/api/documents/' + encodeURIComponent(id);
  const bindDocument = <T extends { document_id: string }>(data: T, id: string, extractionId?: string): T => {
    if (data.document_id !== id || (extractionId && 'extraction_id' in data && data.extraction_id !== extractionId)) {
      throw new ApiError('The processing version changed. Refresh this document.', 409, 'extraction_changed');
    }
    return data;
  };
  return {
    checkAccess: async signal => {
      const result = await request('/api/auth/check', z.object({ authenticated: z.boolean() }), {}, signal);
      if (!result.authenticated) throw new ApiError('Configure DOCUMENT_API_TOKEN on the backend to enable demo sign-in.', 503, 'demo_access_not_configured');
    },
    documents: (query, signal) => request('/api/documents?' + queryString(query as unknown as Record<string, string | number | undefined>), documentListSchema, {}, signal),
    document: async (id, signal) => {
      const data = await request(prefix(id), documentSchema, {}, signal);
      if (data.id !== id) throw new ApiError('The API returned a different document.', 500, 'invalid_response');
      return data;
    },
    upload: (files, signal) => {
      const body = new FormData();
      files.forEach(file => body.append('files', file));
      return request('/api/documents/upload', uploadSchema, { method: 'POST', body }, signal);
    },
    invoice: async (id, signal) => bindDocument(await request(prefix(id) + '/invoice', invoiceResponseSchema, {}, signal), id),
    validation: async (id, extractionId, signal, revision) => {
      const data = bindDocument(await request(prefix(id) + '/validation?' + queryString({ extraction_id: extractionId, correction_revision: revision }), validationSchema, {}, signal), id, extractionId);
      if (revision !== undefined && (data.correction_revision ?? 0) !== revision) throw new ApiError('The invoice was corrected. Refresh the document.', 409, 'correction_changed');
      return data;
    },
    correctInvoice: async (id, command) => {
      const data = bindDocument(await request(prefix(id) + '/corrections', correctionReceiptSchema, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(command) }), id, command.extraction_id);
      if (data.request_id !== command.request_id || data.revision !== command.expected_revision + 1) throw new ApiError('The correction receipt does not match the request. Refresh the document.', 500, 'invalid_response');
      return data;
    },
    corrections: async (id, extractionId, offset, signal) => bindDocument(await request(prefix(id) + '/corrections?' + queryString({ extraction_id: extractionId, offset, limit: 5 }), correctionPageSchema, {}, signal), id, extractionId),
    correction: async (id, extractionId, revision, signal) => {
      const data = bindDocument(await request(prefix(id) + '/corrections/' + revision + '?' + queryString({ extraction_id: extractionId }), correctionDetailSchema, {}, signal), id, extractionId);
      if (data.revision !== revision) throw new ApiError('The correction revision does not match the request.', 500, 'invalid_response');
      return data;
    },
    reprocess: async (id, command) => {
      const data = bindDocument(await request(prefix(id) + '/reprocess', reprocessReceiptSchema, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(command) }), id);
      if (data.request_id !== command.request_id || data.failed_job_id !== command.failed_job_id) throw new ApiError('The retry receipt does not match the request. Refresh the document.', 500, 'invalid_response');
      return data;
    },
    content: async (id, query, signal) => bindDocument(await request(prefix(id) + '/content?' + queryString({ ...query }), contentSchema, {}, signal), id, query.extraction_id),
  };
}
