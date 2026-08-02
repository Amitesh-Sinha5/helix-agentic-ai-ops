import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup, configure } from '@testing-library/react';

// The 1s default is tight for assertions that wait on React state to settle,
// and produced an occasional false failure under load.
configure({ asyncUtilTimeout: 5000 });

/**
 * Install a fake `navigator.clipboard` and restore it afterwards.
 *
 * jsdom has no clipboard, so it has to be defined rather than spied on — and
 * defining it without cleanup leaks a throwing `readText` into later tests.
 */
export function stubClipboard(readText: () => Promise<string>) {
  const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
  Object.defineProperty(navigator, 'clipboard', {
    value: { readText },
    configurable: true,
    writable: true,
  });
  clipboardRestorers.push(() => {
    if (original) Object.defineProperty(navigator, 'clipboard', original);
    else Reflect.deleteProperty(navigator as object, 'clipboard');
  });
}

const clipboardRestorers: Array<() => void> = [];

// jsdom implements neither of these, and both are load-bearing in the app.
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  closed = false;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  /** Test helper: simulate the server accepting the connection. */
  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  /** Test helper: push a frame to the client. */
  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  close() {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.();
  }
}

vi.stubGlobal('WebSocket', MockWebSocket);
export { MockWebSocket };

if (!globalThis.crypto?.getRandomValues) {
  vi.stubGlobal('crypto', {
    getRandomValues: (array: Uint8Array) => {
      for (let i = 0; i < array.length; i += 1) array[i] = Math.floor(Math.random() * 256);
      return array;
    },
  });
}

// Recharts measures its container, which jsdom reports as 0x0.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

afterEach(() => {
  while (clipboardRestorers.length) clipboardRestorers.pop()!();
  cleanup();
  localStorage.clear();
  MockWebSocket.instances.length = 0;
  vi.restoreAllMocks();
});
