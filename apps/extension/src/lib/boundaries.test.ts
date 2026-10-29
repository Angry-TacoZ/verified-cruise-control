import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

// Vitest runs with cwd set to the extension package root.
const root = process.cwd();

function read(rel: string): string {
  return fs.readFileSync(path.join(root, rel), 'utf-8');
}

describe('security boundaries (static guards)', () => {
  it('content script never touches the pairing token store', () => {
    const source = read('entrypoints/listing.content.ts');
    expect(source).not.toMatch(/pairingToken/i);
    expect(source).not.toMatch(/store\.ts|from 'wxt\/utils\/storage'/);
    expect(source).not.toMatch(/X-Extension-Token/);
  });

  it('manifest permissions stay narrowly scoped', () => {
    const config = read('wxt.config.ts');
    expect(config).not.toContain('<all_urls>');
    expect(config).not.toContain('chrome-extension://*');
    expect(config).toContain('http://127.0.0.1:8000/*');
    expect(config).toContain('http://127.0.0.1:8901/*');
  });

  it('extractor registry has no wildcard hosts', () => {
    const source = read('src/lib/extractors.ts');
    expect(source).not.toContain('*://');
    expect(source).not.toContain('<all_urls>');
  });
});
