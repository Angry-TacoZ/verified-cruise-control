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
    expect(config).not.toMatch(/['"]scripting['"]/);
    expect(config).toContain('http://127.0.0.1:8000/*');
    expect(config).toContain('http://127.0.0.1:8901/*');
  });

  it('background restricts storage.local before handling messages', () => {
    const source = read('entrypoints/background.ts');
    const restrictAt = source.indexOf('ensureTrustedStorage()');
    const listenAt = source.indexOf('addListener');
    expect(restrictAt).toBeGreaterThanOrEqual(0);
    expect(listenAt).toBeGreaterThan(restrictAt);
  });

  it('pairing token reads gate on the storage restriction', () => {
    const source = read('src/lib/store.ts');
    expect(source).toContain('TRUSTED_CONTEXTS');
    const getter = source.slice(source.indexOf('getPairingToken'));
    expect(getter).toContain('await ensureTrustedStorage()');
  });

  it('storage restriction fails closed instead of silently falling back', () => {
    const source = read('src/lib/store.ts');
    expect(source).toContain('StorageRestrictionError');
    // No silent swallow: every failure path rejects.
    expect(source).not.toMatch(/catch\s*\{\s*}/);
    expect(source).not.toMatch(/without the API.*resolves without/i);
  });

  it('pairing token never crosses the content-script boundary', () => {
    const background = read('entrypoints/background.ts');
    // The token travels only to localhost API calls, never in messages.
    expect(background).not.toMatch(/sendMessage\([^)]*token/i);
    const protocol = read('src/lib/protocol.ts');
    // No message shape declares a credential field (the FORBIDDEN_KEYS list
    // names them only to reject them).
    expect(protocol).not.toMatch(/['"]?(pairingToken|extensionToken)['"]?\s*:/);
  });

  it('extractor registry has no wildcard hosts', () => {
    const source = read('src/lib/extractors.ts');
    expect(source).not.toContain('*://');
    expect(source).not.toContain('<all_urls>');
  });
});
