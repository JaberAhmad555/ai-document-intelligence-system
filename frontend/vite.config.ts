import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const base = env.VITE_API_BASE_URL?.trim();
  if (command === 'build' && !process.env.VITEST) {
    if (!base) throw new Error('Set VITE_API_BASE_URL to / or the hosted backend origin.');
    if (base !== '/') {
      const url = new URL(base);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
        throw new Error('VITE_API_BASE_URL must be a public HTTP(S) origin, without credentials.');
      }
      if (['localhost', '127.0.0.1', '[::1]', '0.0.0.0'].includes(url.hostname)) {
        throw new Error('Production builds cannot embed a loopback API address. Use / for local proxy previews, or a hosted backend origin.');
      }
    }
  }
  const proxy = {
    '/api': { target: env.DEV_API_PROXY_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
  };
  const test = {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/test/**/*.test.ts', 'src/test/**/*.test.tsx'],
    clearMocks: true,
    restoreMocks: true,
  };
  return {
    plugins: [react()],
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
    build: { sourcemap: false },
    test,
  };
});
