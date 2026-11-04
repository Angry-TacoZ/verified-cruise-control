import { describe, expect, it } from 'vitest';
import {
  autotraderExtractor,
  extractorForUrl,
  genericExtractor,
  syntheticExtractor,
} from './extractors';

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
    // Unknown hosts fall through to the generic extractor (null only for
    // non-web URLs now); the generic rule itself refuses non-listings.
    expect(extractorForUrl('https://other.test/')?.id).toBe('generic-v1');
    expect(extractorForUrl('chrome://extensions/')).toBeNull();
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

describe('generic extractor', () => {
  it('matches any http(s) page without hostname rules', () => {
    for (const url of [
      'https://motors.example/listings/1',
      'https://www.cargurus.example/details/2',
      'https://cars.example/vdp/3',
      'http://dealer.example:8080/inventory/42',
    ]) {
      expect(genericExtractor.matches(url)).toBe(true);
      expect(extractorForUrl(url)?.id).toBe('generic-v1');
    }
    expect(genericExtractor.matches('chrome://extensions/')).toBe(false);
    // Site precision rules still win where they match.
    expect(extractorForUrl(VDP_URL)?.id).toBe('autotrader-v1');
  });

  it('1. extracts a Schema.org listing on an unknown site', () => {
    const html = `<!doctype html><html><head><title>2022 Mazda CX-5</title>
<meta property="og:title" content="2022 Mazda CX-5 Touring" />
<meta property="og:image" content="https://motors.example/hero.jpg" />
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Car",
"name":"2022 Mazda CX-5 Touring","modelDate":"2022","manufacturer":"Mazda",
"model":"CX-5","vehicleConfiguration":"Touring",
"vehicleIdentificationNumber":"JM3KFBDMXN0000001",
"vehicleFeature":["Adaptive Cruise Control","Blind Spot Monitoring"],
"image":["https://motors.example/a.jpg"]}</script>
</head><body><h1>2022 Mazda CX-5 Touring</h1>
<div class="gallery"><img src="https://motors.example/b.jpg" /></div></body></html>`;
    const listing = genericExtractor.extract(doc(html), 'https://motors.example/listings/9');
    expect(listing?.vin).toBe('JM3KFBDMXN0000001');
    expect(listing?.make).toBe('Mazda');
    expect(listing?.advertisedClaims).toContain('Adaptive Cruise Control');
    const urls = (listing?.photos ?? []).map((p) => p.url);
    expect(urls).toEqual([
      'https://motors.example/a.jpg',
      'https://motors.example/hero.jpg',
      'https://motors.example/b.jpg',
    ]);
    expect(listing?.extraction?.confidence).toBe('high');
    expect(listing?.extraction?.fieldSources['vin']).toBe('structured');
    expect(listing?.extraction?.fieldSources['photos']).toBe('structured');
  });

  it('2. handles a dynamic page with generic DOM markup', () => {
    const html = `<!doctype html><html><head><title>Listing</title></head><body>
<h1>2019 Subaru Outback 2.5i Premium</h1>
<dl><dt>Year</dt><dd>2019</dd><dt>Make</dt><dd>Subaru</dd><dt>Model</dt><dd>Outback</dd></dl>
<ul class="equipment-list"><li>Adaptive Cruise Control</li><li>Roof Rails</li></ul>
<div id="gallery">
<img data-src="https://auto.example/lazy1.jpg" />
<img src="https://auto.example/s.jpg" srcset="https://auto.example/s.jpg 640w, https://auto.example/l.jpg 1600w" />
</div></body></html>`;
    const listing = genericExtractor.extract(doc(html), 'https://auto.example/v/1');
    expect(listing?.year).toBe(2019);
    expect(listing?.make).toBe('Subaru');
    expect(listing?.model).toBe('Outback');
    expect(listing?.advertisedClaims).toContain('Adaptive Cruise Control');
    const urls = (listing?.photos ?? []).map((p) => p.url);
    expect(urls).toContain('https://auto.example/lazy1.jpg');
    expect(urls).toContain('https://auto.example/l.jpg');
    expect(urls).not.toContain('https://auto.example/s.jpg');
    expect(listing?.extraction?.fieldSources['make']).toBe('dom');
  });

  it('3. accepts incomplete dealer metadata with low confidence', () => {
    const html = `<!doctype html><html><head><title>2018 Ford Focus</title></head><body>
<h1>2018 Ford Focus SE Hatchback</h1>
<img src="https://dealer.example/p1.jpg" /><img src="https://dealer.example/p2.jpg" />
</body></html>`;
    const listing = genericExtractor.extract(doc(html), 'https://dealer.example/car/5');
    expect(listing).not.toBeNull();
    expect(listing?.year).toBe(2018);
    expect(listing?.model).toBe('');
    expect(listing?.extraction?.confidence).toBe('low');
  });

  it('4. excludes logos, ads, and recommended vehicles', () => {
    const html = `<!doctype html><html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Car",
"name":"2021 Kia K5 GT-Line","modelDate":"2021","manufacturer":"Kia","model":"K5"}</script>
</head><body><header><img src="https://cars.example/logo.png" alt="Dealer logo" /></header>
<h1>2021 Kia K5 GT-Line</h1>
<img src="https://cars.example/main.jpg" alt="Kia K5 front" />
<div class="advertisement"><img src="https://ads.example/banner.jpg" /></div>
<section class="recommended-vehicles"><h2>You may also like</h2>
<img src="https://cars.example/other-car.jpg" alt="2020 Hyundai Sonata" /></section>
<img src="https://cars.example/pixel.gif" width="1" height="1" />
</body></html>`;
    const listing = genericExtractor.extract(doc(html), 'https://cars.example/vdp/7');
    const urls = (listing?.photos ?? []).map((p) => p.url);
    expect(urls).toContain('https://cars.example/main.jpg');
    expect(urls).not.toContain('https://cars.example/logo.png');
    expect(urls).not.toContain('https://ads.example/banner.jpg');
    expect(urls).not.toContain('https://cars.example/other-car.jpg');
    expect(urls).not.toContain('https://cars.example/pixel.gif');
  });

  it('5. refuses a page without a vehicle', () => {
    const html = `<!doctype html><html><head><title>Blog</title></head><body>
<article><h1>Why I love road trips</h1><p>My car is great.</p>
<img src="https://blog.example/road.jpg" alt="car on road" /></article></body></html>`;
    expect(genericExtractor.extract(doc(html), 'https://blog.example/post/1')).toBeNull();
  });

  it('6. keeps cross-origin CDN gallery URLs for permission handling', () => {
    const html = `<!doctype html><html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Vehicle",
"name":"2020 BMW 330i","modelDate":"2020","manufacturer":"BMW","model":"330i",
"vehicleIdentificationNumber":"WBA5R000000000001"}</script>
</head><body><h1>2020 BMW 330i</h1>
<img src="https://cdn.photos.example/u/1.jpg" />
<img src="https://img.other-cdn.example/u/2.jpg" /></body></html>`;
    const listing = genericExtractor.extract(doc(html), 'https://market.example/l/3');
    const urls = (listing?.photos ?? []).map((p) => p.url);
    expect(urls).toContain('https://cdn.photos.example/u/1.jpg');
    expect(urls).toContain('https://img.other-cdn.example/u/2.jpg');
    expect(listing?.extraction?.confidence).toBe('high');
  });

  it('7. keeps only usable photos from a partially blocked gallery', () => {
    const html = `<!doctype html><html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Car",
"name":"2017 VW Golf","modelDate":"2017","manufacturer":"Volkswagen","model":"Golf"}</script>
</head><body><h1>2017 VW Golf</h1>
<img src="data:image/jpeg;base64,/9j/4AAQ" />
<img src="javascript:alert(1)" />
<img src="blob:https://outlet.example/uuid" />
<img src="https://outlet.example/ok.jpg" /></body></html>`;
    const listing = genericExtractor.extract(doc(html), 'https://outlet.example/l/4');
    expect((listing?.photos ?? []).map((p) => p.url)).toEqual(['https://outlet.example/ok.jpg']);
  });

  it('rejects search and multi-vehicle index pages', () => {
    const search = `<!doctype html><html><body><div class="search-results">
<div>car 1</div><div>car 2</div></div></body></html>`;
    expect(
      genericExtractor.extract(doc(search), 'https://motors.example/search?q=civic'),
    ).toBeNull();
    const index = `<!doctype html><html><head><script type="application/ld+json">
[{"@context":"https://schema.org","@type":"Car","name":"Car A",
"vehicleIdentificationNumber":"VINAAAA00000000001"},
{"@context":"https://schema.org","@type":"Car","name":"Car B",
"vehicleIdentificationNumber":"VINBBBB00000000002"}]</script>
</head><body><h1>Inventory</h1></body></html>`;
    expect(genericExtractor.extract(doc(index), 'https://dealer.example/cars')).toBeNull();
  });
});
