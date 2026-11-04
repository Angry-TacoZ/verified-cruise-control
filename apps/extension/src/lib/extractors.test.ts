import { describe, expect, it } from 'vitest';
import { autotraderExtractor, extractorForUrl, syntheticExtractor } from './extractors';

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

const AUTOTRADER_VDP = `<!doctype html><html><head>
<title>2021 Honda Accord EX-L - $24,000 | Autotrader</title>
<link rel="canonical" href="https://www.autotrader.com/cars-for-sale/vehicle/2021-honda-accord-atx-12345?resultSetId=abc&amp;searchUuid=def" />
<meta property="og:url" content="https://www.autotrader.com/cars-for-sale/vehicle/2021-honda-accord-atx-12345" />
<meta property="og:title" content="2021 Honda Accord EX-L" />
<meta property="og:description" content="One-owner Accord with driver aids." />
<meta property="og:image" content="https://images.autotrader.com/hero.jpg" />
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Car","name":"2021 Honda Accord EX-L",
 "modelDate":"2021","manufacturer":{"@type":"Organization","name":"Honda"},
 "model":"Accord","vehicleConfiguration":"EX-L",
 "vehicleIdentificationNumber":"1HGCV1F30MA000001",
 "description":"One-owner Accord with driver aids.",
 "image":["https://images.autotrader.com/1.jpg",{"url":"https://images.autotrader.com/2.jpg"}],
 "vehicleFeature":["Adaptive Cruise Control","Lane Keeping Assist System","Bluetooth"]}
</script></head><body>
<h1>2021 Honda Accord EX-L</h1>
<div class="gallery">
<img src="/thumb.jpg" data-src="https://images.autotrader.com/lazy.jpg" />
<img src="https://images.autotrader.com/small.jpg"
     srcset="https://images.autotrader.com/small.jpg 640w, https://images.autotrader.com/big.jpg 1600w" />
<img src="javascript:alert(1)" />
</div></body></html>`;

const VDP_URL = 'https://www.autotrader.com/cars-for-sale/vehicle/2021-honda-accord-atx-12345';

describe('autotrader extractor', () => {
  it('matches autotrader hosts only', () => {
    expect(autotraderExtractor.matches(VDP_URL)).toBe(true);
    expect(autotraderExtractor.matches('https://autotrader.com/cars-for-sale/vehicle/1')).toBe(
      true,
    );
    expect(autotraderExtractor.matches('https://www.cargurus.com/details/1')).toBe(false);
    expect(autotraderExtractor.matches(URL)).toBe(false);
    expect(extractorForUrl(VDP_URL)?.id).toBe('autotrader-v1');
  });

  it('extracts vehicle, claims, and photos from a VDP', () => {
    const listing = autotraderExtractor.extract(doc(AUTOTRADER_VDP), VDP_URL);
    expect(listing).not.toBeNull();
    expect(listing?.year).toBe(2021);
    expect(listing?.make).toBe('Honda');
    expect(listing?.model).toBe('Accord');
    expect(listing?.trim).toBe('EX-L');
    expect(listing?.vin).toBe('1HGCV1F30MA000001');
    expect(listing?.title).toBe('2021 Honda Accord EX-L');
    expect(listing?.extractorId).toBe('autotrader-v1');
    expect(listing?.sourceSite).toBe('www.autotrader.com');
    // Seller wording preserved verbatim, including ACC when advertised.
    expect(listing?.advertisedClaims).toContain('Adaptive Cruise Control');
    expect(listing?.advertisedClaims).toContain('Bluetooth');
    // JSON-LD images first, then the largest srcset variant, lazy, og:image.
    const urls = (listing?.photos ?? []).map((p) => p.url);
    expect(urls).toContain('https://images.autotrader.com/1.jpg');
    expect(urls).toContain('https://images.autotrader.com/2.jpg');
    expect(urls).toContain('https://images.autotrader.com/big.jpg');
    expect(urls).not.toContain('https://images.autotrader.com/small.jpg');
    expect(urls).toContain('https://images.autotrader.com/lazy.jpg');
    expect(urls).not.toContain('javascript:alert(1)');
  });

  it('strips session params for a stable source identity', () => {
    const listing = autotraderExtractor.extract(
      doc(AUTOTRADER_VDP),
      `${VDP_URL}?resultSetId=abc&searchUuid=def&utm_source=x`,
    );
    expect(listing?.sourceUrl).toBe(VDP_URL);
  });

  it('rejects search-results pages', () => {
    const searchHtml = `<!doctype html><html><head><title>Search</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"SearchResultsPage"}</script>
</head><body><div class="search-results"><div>car 1</div><div>car 2</div></div></body></html>`;
    const searchUrl = 'https://www.autotrader.com/cars-for-sale/search?zip=12345';
    expect(autotraderExtractor.extract(doc(searchHtml), searchUrl)).toBeNull();
    expect(
      autotraderExtractor.extract(doc('<html><body>nope</body></html>'), searchUrl),
    ).toBeNull();
  });

  it('keeps generic cruise distinct from ACC and never synthesizes claims', () => {
    const html = `<!doctype html><html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Car",
 "name":"2019 Ford Fusion SE","modelDate":"2019",
 "manufacturer":"Ford","model":"Fusion",
 "vehicleFeature":["Cruise Control","SYNC"]}</script>
</head><body><h1>2019 Ford Fusion SE</h1></body></html>`;
    const listing = autotraderExtractor.extract(
      doc(html),
      'https://www.autotrader.com/cars-for-sale/vehicle/2019-ford-fusion-9',
    );
    expect(listing?.advertisedClaims).toContain('Cruise Control');
    expect((listing?.advertisedClaims ?? []).some((c) => /adaptive/i.test(c))).toBe(false);
  });

  it('returns null when no vehicle signal exists', () => {
    expect(
      autotraderExtractor.extract(doc('<html><head></head><body>hello</body></html>'), VDP_URL),
    ).toBeNull();
  });

  it('skips malformed JSON-LD and uses DOM fallbacks', () => {
    const html = `<!doctype html><html><head>
<script type="application/ld+json">{"@type": broken</script>
<meta property="og:title" content="2020 Toyota Camry SE" />
</head><body><h1>2020 Toyota Camry SE</h1>
<img src="https://images.autotrader.com/only.jpg" /></body></html>`;
    const listing = autotraderExtractor.extract(doc(html), VDP_URL);
    expect(listing?.title).toBe('2020 Toyota Camry SE');
    expect(listing?.year).toBe(2020);
    expect(listing?.photos).toEqual([{ url: 'https://images.autotrader.com/only.jpg' }]);
  });
});
