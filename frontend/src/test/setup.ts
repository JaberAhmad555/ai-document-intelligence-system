import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
if (!window.requestAnimationFrame) {
  window.requestAnimationFrame = callback => window.setTimeout(() => callback(performance.now()), 0);
  window.cancelAnimationFrame = id => window.clearTimeout(id);
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
