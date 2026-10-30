import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { App } from './App';
import { ApiError, type UploadLimits } from './api';
import { createDemoSession, type DemoSession } from './demoSession';
import './access.css';

export const DEMO_IDLE_TIMEOUT_MS = 30 * 60 * 1000;

export function DemoAccess({ baseUrl, limits }: { baseUrl: string | undefined; limits: UploadLimits }) {
  const [session, setSession] = useState<DemoSession | null>(null);
  const active = useRef<DemoSession | null>(null);
  const attempt = useRef<DemoSession | null>(null);
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const passwordInput = useRef<HTMLInputElement>(null);

  const lock = useCallback((reason: string) => {
    active.current?.dispose(); attempt.current?.dispose();
    active.current = null; attempt.current = null;
    setSession(null); setPassword(''); setPending(false); setError(''); setMessage(reason);
  }, []);

  useEffect(() => {
    const pageHidden = () => lock('The workspace was locked when you left this page.');
    const pageShown = (event: PageTransitionEvent) => { if (event.persisted) lock('Enter the demo password again to reopen the workspace.'); };
    window.addEventListener('pagehide', pageHidden);
    window.addEventListener('pageshow', pageShown);
    return () => {
      window.removeEventListener('pagehide', pageHidden); window.removeEventListener('pageshow', pageShown);
      active.current?.dispose(); attempt.current?.dispose();
      active.current = null; attempt.current = null;
    };
  }, [lock]);

  useEffect(() => { if (!session && !pending) passwordInput.current?.focus(); }, [session, pending]);

  useEffect(() => {
    if (!session) return;
    let deadline = Date.now() + DEMO_IDLE_TIMEOUT_MS;
    let timer: number;
    const check = () => { if (Date.now() >= deadline) lock('Workspace locked after 30 minutes without activity. Enter the demo password to continue.'); };
    const touch = () => {
      if (Date.now() >= deadline) { check(); return; }
      deadline = Date.now() + DEMO_IDLE_TIMEOUT_MS;
      window.clearTimeout(timer); timer = window.setTimeout(check, DEMO_IDLE_TIMEOUT_MS);
    };
    timer = window.setTimeout(check, DEMO_IDLE_TIMEOUT_MS);
    window.addEventListener('pointerdown', touch, { passive: true });
    window.addEventListener('keydown', touch);
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    return () => {
      window.clearTimeout(timer); window.removeEventListener('pointerdown', touch);
      window.removeEventListener('keydown', touch); window.removeEventListener('focus', check);
      document.removeEventListener('visibilitychange', check);
    };
  }, [session, lock]);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    if (attempt.current || pending) return;
    setError(''); setMessage('');
    const submitted = password;
    setPassword('');
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(submitted)) { setError('Enter the exact demo password provided with your invitation.'); return; }
    let next: DemoSession;
    try {
      next = createDemoSession(baseUrl, submitted, () => {
        if (active.current === next || attempt.current === next) lock('Demo access is no longer valid. Enter the current demo password.');
      });
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Check the API configuration.'); return; }
    attempt.current = next; setPending(true);
    try {
      // This endpoint performs no document lookup, upload, job creation or LLM call.
      await next.api.checkAccess();
      if (attempt.current !== next) return;
      active.current = next; attempt.current = null; setSession(next); setPending(false);
    } catch (cause) {
      if (attempt.current !== next) return;
      next.dispose(); attempt.current = null; setPending(false);
      setError(cause instanceof ApiError && cause.status === 401
        ? 'The demo password was not accepted. Try the current password.'
        : cause instanceof Error ? cause.message : 'Could not verify demo access. Please try again.');
    }
  }

  if (session) return <App api={session.api} limits={limits} onSignOut={() => lock('Workspace locked. Your password and document view have been cleared.')} />;

  return <main className="access-screen"><section className="access-card" aria-labelledby="access-title">
    <a className="brand access-brand" href="#/" aria-label="Document Intelligence"><span className="brand-mark" aria-hidden="true">di<span>·</span></span><span>Document<br />Intelligence</span></a>
    <p className="eyebrow">CARE GUIDE · ASSESSMENT DEMO</p>
    <h1 id="access-title">Open the review workspace</h1>
    <p className="access-intro">Enter the demo password shared with your invitation to review documents, inspect evidence, and try the processing workflow.</p>
    <form onSubmit={event => void signIn(event)} aria-label="Demo access">
      <label htmlFor="demo-password">Demo password</label>
      <input ref={passwordInput} id="demo-password" name="demo-password" type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={256} required disabled={pending} value={password} onChange={event => setPassword(event.target.value)} aria-describedby="access-memory-note" />
      {message && <p className="access-notice" role="status">{message}</p>}
      {error && <p className="access-error" role="alert">{error}</p>}
      <button className="button button-primary access-submit" type="submit" disabled={pending || !password}>{pending ? 'Checking access…' : 'Open workspace'}</button>
      {pending && <p className="access-wait" role="status">Connecting securely. The demo server may take a moment to respond.</p>}
    </form>
    <p className="access-memory" id="access-memory-note">Your password stays in this tab’s memory. Reloading, leaving the page, locking the workspace, or 30 minutes of inactivity requires you to enter it again.</p>
    <div className="access-footer"><span>Document operations console</span><span>Reviewer access</span></div>
  </section></main>;
}
