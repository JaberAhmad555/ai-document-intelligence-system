/** In-memory credential lifetime. No storage, cookie, URL or frontend env token. */
import { createApi, normalizeBase, type DemoApi } from './api';

export interface DemoSession { api: DemoApi; dispose(): void }

export function checkSecureApiLocation(baseUrl: string | undefined): void {
  const base = normalizeBase(baseUrl);
  const target = new URL(base || '/', window.location.origin);
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(target.hostname);
  if (target.protocol !== 'https:' && !(target.protocol === 'http:' && loopback)) {
    throw new Error('Demo access requires an HTTPS backend. HTTP is allowed only on localhost for development.');
  }
}

export function createDemoSession(baseUrl: string | undefined, password: string, onUnauthorized: () => void): DemoSession {
  checkSecureApiLocation(baseUrl);
  let credential: string | null = password;
  const lifetime = new AbortController();
  const api = createApi(baseUrl, {
    getToken: () => credential,
    requireAuthentication: true,
    sessionSignal: lifetime.signal,
    onUnauthorized,
  });
  return { api, dispose() { credential = null; lifetime.abort(); } };
}
