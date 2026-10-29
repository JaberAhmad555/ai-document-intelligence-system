import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { ApiError } from './api';

interface State<T> { key: string; data?: T; error?: Error; loading: boolean; refreshing: boolean; updatedAt?: Date }
interface Options<T> { key: string; load: (signal: AbortSignal) => Promise<T>; enabled?: boolean; poll?: (data: T | undefined) => number | false }
export function useRemote<T>({ key, load, enabled = true, poll }: Options<T>) {
  const [state, setState] = useState<State<T>>({ key, loading: true, refreshing: false });
  const [revision, setRevision] = useState(0);
  const loadRef = useRef(load); loadRef.current = load;
  const pollRef = useRef(poll); pollRef.current = poll;
  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let controller: AbortController | undefined;
    let timer: number | undefined;
    let failures = 0;
    const schedule = (delay: number) => { window.clearTimeout(timer); timer = window.setTimeout(run, delay); };
    async function run() {
      if (disposed) return;
      if (document.visibilityState === 'hidden') { schedule(4000); return; }
      window.clearTimeout(timer);
      controller?.abort();
      const current = new AbortController(); controller = current;
      setState(previous => ({ ...previous, key, data: previous.key === key ? previous.data : undefined,
        error: undefined, updatedAt: previous.key === key ? previous.updatedAt : undefined, loading: previous.key !== key || previous.data === undefined, refreshing: true }));
      try {
        const data = await loadRef.current(current.signal);
        if (disposed || current.signal.aborted) return;
        failures = 0;
        setState({ key, data, loading: false, refreshing: false, updatedAt: new Date() });
        const delay = pollRef.current?.(data);
        if (delay) schedule(delay);
      } catch (cause) {
        if (disposed || current.signal.aborted) return;
        const error = cause instanceof Error ? cause : new Error('Request failed.');
        setState(previous => ({ ...previous, key, error, loading: false, refreshing: false }));
        failures += 1;
        // Do not repeatedly request terminal 404/409 or invalid-contract results.
        if (!(error instanceof ApiError) || error.status === 0 || error.status === 429 || error.status >= 500) {
          schedule(Math.min(30000, 4000 * 2 ** Math.min(failures - 1, 3)));
        }
      }
    }
    const resume = () => { if (document.visibilityState !== 'hidden') void run(); };
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('online', resume);
    void run();
    return () => { disposed = true; controller?.abort(); window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', resume); window.removeEventListener('online', resume); };
  }, [key, enabled, revision]);
  const visible: State<T> = state.key === key ? state : { key, loading: true, refreshing: false };
  return { ...visible, loading: enabled && visible.loading, refresh: () => setRevision(value => value + 1) };
}
const subscribe = (callback: () => void) => { window.addEventListener('hashchange', callback); return () => window.removeEventListener('hashchange', callback); };
export function useLocationHash() { return useSyncExternalStore(subscribe, () => window.location.hash || '#/', () => '#/'); }
export function documentHref(id: string) { return '#/documents/' + encodeURIComponent(id); }
export function parseRoute(hash: string): { kind: 'list' } | { kind: 'document'; id: string } | { kind: 'missing' } {
  const path = hash.replace(/^#/, '').split('?')[0];
  if (path === '/' || path === '') return { kind: 'list' };
  const match = /^\/documents\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.exec(path);
  return match ? { kind: 'document', id: match[1] } : { kind: 'missing' };
}
