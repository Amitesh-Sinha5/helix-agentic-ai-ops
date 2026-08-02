/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // bind 0.0.0.0 so the container port mapping works
    port: 5173,
  },
  preview: { host: true, port: 5173 },
  build: { outDir: 'dist', sourcemap: true },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    // Playwright specs live in e2e/ and must not be collected by Vitest.
    exclude: ['node_modules/**', 'dist/**', 'e2e/**'],
    coverage: { reporter: ['text', 'lcov'], include: ['src/**/*.{ts,tsx}'] },
  },
});
