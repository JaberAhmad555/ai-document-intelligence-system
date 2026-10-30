import { StrictMode, Component, type ErrorInfo, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { DemoAccess } from './DemoAccess';
import { checkSecureApiLocation } from './demoSession';
import { readLimits } from './api';
import './styles.css';

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(_error: Error, _info: ErrorInfo) { /* Avoid logging document content. */ }
  render() {
    return this.state.failed ? <main className="setup-screen"><h1>This view could not be displayed</h1><p>Reload to retrieve the document again.</p><button className="button" onClick={() => window.location.reload()}>Reload console</button></main> : this.props.children;
  }
}
const root = createRoot(document.getElementById('root')!);
try {
  const baseUrl = import.meta.env.VITE_API_BASE_URL;
  checkSecureApiLocation(baseUrl);
  const limits = readLimits(import.meta.env);
  root.render(<StrictMode><Boundary><DemoAccess baseUrl={baseUrl} limits={limits} /></Boundary></StrictMode>);
} catch (error) {
  root.render(<main className="setup-screen"><p className="eyebrow">Document Intelligence</p><h1>Configuration required</h1><p>{error instanceof Error ? error.message : 'Check frontend environment settings.'}</p></main>);
}
