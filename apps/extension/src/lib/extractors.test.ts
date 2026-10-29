import { describe, expect, it } from 'vitest';
import { extractorForUrl, syntheticExtractor } from './extractors';

const FIXTURE_HTML = `<!doctype html><html><head><title>Fallback title</title></head><body>
<div data-vcc-listing data-vcc-title="1987 Test Coupe LX" data-vcc-year="1987"
     data-vcc-make="Test" data-vcc-model="Coupe" data-vcc-trim="LX" data-vcc-vin="">
  <span data-vcc-source-site="example-motors.test"></span>
  <p data-vcc-description>A synthetic test listing.</p>
  <ul><li data-vcc-claim>cruise control</li><li data-vcc-claim>lane keeping assist</li></ul>
  <img data-vcc-photo src="/photos/front.jpg" />
  <img data-vcc-photo src="https://cdn.example.test/photos/rear.png" />
  <img data-vcc-photo src="javascript:alert(1)" />
</div></body></html>`;

const URL = 'http://127.0.0.1:8901/listings/1987-test-coupe.html';

function doc(html: string): Document {
  return new DOMParser().parseFromString(html, 'text/html');
}

describe('synthetic extractor', () => {
  it('matches only the fixture server', () => {
    expect(syntheticExtractor.matches(URL)).toBe(true);
    expect(syntheticExtractor.matches('https://dealership.example/l/1')).toBe(false);
    expect(syntheticExtractor.matches('http://127.0.0.1:8000/api/health')).toBe(false);
    expect(extractorForUrl(URL)?.id).toBe('synthetic-v1');
    expect(extractorForUrl('https://other.test/')).toBeNull();
  });

  it('extracts listing metadata and claims', () => {
    const listing = syntheticExtractor.extract(doc(FIXTURE_HTML), URL);
    expect(listing).not.toBeNull();
    expect(listing?.title).toBe('1987 Test Coupe LX');
    expect(listing?.year).toBe(1987);
    expect(listing?.make).toBe('Test');
    expect(listing?.sourceUrl).toBe(URL);
    expect(listing?.sourceSite).toBe('example-motors.test');
    expect(listing?.vin).toBeNull();
    expect(listing?.description).toBe('A synthetic test listing.');
    expect(listing?.advertisedClaims).toEqual(['cruise control', 'lane keeping assist']);
    expect(listing?.extractorId).toBe('synthetic-v1');
  });

  it('resolves relative photo URLs and drops dangerous schemes', () => {
    const listing = syntheticExtractor.extract(doc(FIXTURE_HTML), URL);
    expect(listing?.photos).toEqual([
      { url: 'http://127.0.0.1:8901/photos/front.jpg' },
      { url: 'https://cdn.example.test/photos/rear.png' },
    ]);
  });

  it('returns null when the listing marker is absent', () => {
    expect(syntheticExtractor.extract(doc('<html><body>nope</body></html>'), URL)).toBeNull();
  });

  it('caps photos and claims at backend limits', () => {
    const many = Array.from(
      { length: 60 },
      (_, i) => `<img data-vcc-photo src="/p${i}.jpg" />`,
    ).join('');
    const claims = Array.from({ length: 250 }, (_, i) => `<li data-vcc-claim>c${i}</li>`).join('');
    const listing = syntheticExtractor.extract(
      doc(`<html><body><div data-vcc-listing>${many}<ul>${claims}</ul></div></body></html>`),
      URL,
    );
    expect(listing?.photos).toHaveLength(40);
    expect(listing?.advertisedClaims).toHaveLength(200);
  });
});
