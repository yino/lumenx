import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// Node 26 exposes an unusable global localStorage unless a backing file is
// configured. Tests need deterministic, process-local storage in both the
// plain Node and browser-like environments.
const storageData = new Map<string, string>();
const testLocalStorage: Storage = {
    get length() {
        return storageData.size;
    },
    clear() {
        storageData.clear();
    },
    getItem(key) {
        return storageData.get(String(key)) ?? null;
    },
    key(index) {
        return Array.from(storageData.keys())[index] ?? null;
    },
    removeItem(key) {
        storageData.delete(String(key));
    },
    setItem(key, value) {
        storageData.set(String(key), String(value));
    },
};

Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: testLocalStorage,
});
if (typeof window !== 'undefined' && window !== globalThis) {
    Object.defineProperty(window, 'localStorage', {
        configurable: true,
        value: testLocalStorage,
    });
}

afterEach(() => {
    cleanup();
});
