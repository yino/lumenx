import { readFileSync, readdirSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { describe, expect, it } from 'vitest';

const SOURCE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SOURCE_EXTENSIONS = new Set(['.ts', '.tsx']);

// In atelier-dark, primary is near-white. Opaque primary surfaces must use
// the theme-aware contrast token instead of white or the regular foreground.
const WRONG_PRIMARY_FOREGROUND = /(?:\bbg-primary(?:\/(?:[89]\d|100)\b|(?![-/\[]))[^"'`\n]*\btext-(?:white|foreground)\b|\btext-(?:white|foreground)\b[^"'`\n]*\bbg-primary(?:\/(?:[89]\d|100)\b|(?![-/\[])))/g;

function collectSourceFiles(directory: string): string[] {
    return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
        const filePath = path.join(directory, entry.name);
        if (entry.isDirectory()) return collectSourceFiles(filePath);
        return SOURCE_EXTENSIONS.has(path.extname(entry.name)) ? [filePath] : [];
    });
}

describe('primary surface contrast', () => {
    it('uses text-on-accent on opaque and high-opacity primary surfaces', () => {
        const violations = collectSourceFiles(SOURCE_ROOT).flatMap((filePath) =>
            readFileSync(filePath, 'utf8')
                .split('\n')
                .flatMap((line, index) => {
                    WRONG_PRIMARY_FOREGROUND.lastIndex = 0;
                    return WRONG_PRIMARY_FOREGROUND.test(line)
                        ? [`${path.relative(SOURCE_ROOT, filePath)}:${index + 1}`]
                        : [];
                }),
        );

        expect(violations, 'Replace the foreground with text-on-accent').toEqual([]);
    });
});
