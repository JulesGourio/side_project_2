import { beforeAll, describe, expect, test } from 'bun:test';

let isSafeLink: (href: string | undefined | null) => boolean;
let isSafeImage: (src: string | undefined | null) => boolean;

beforeAll(async () => {
  (globalThis as { window?: unknown }).window = { location: { origin: 'https://app.test' } };
  ({ isSafeLink, isSafeImage } = await import('../src/components/shared/safeUrl'));
});

describe('isSafeLink', () => {
  test('only https links are clickable', () => {
    expect(isSafeLink('https://example.com/doc')).toBe(true);
    expect(isSafeLink('http://example.com')).toBe(false);
    expect(isSafeLink('javascript:alert(1)')).toBe(false);
    expect(isSafeLink('data:text/html;base64,AAAA')).toBe(false);
    expect(isSafeLink('')).toBe(false);
    expect(isSafeLink(undefined)).toBe(false);
  });
});

describe('isSafeImage', () => {
  test('inline raster images and the app origin load, other hosts do not', () => {
    expect(isSafeImage('data:image/png;base64,iVBORw0KGgo=')).toBe(true);
    expect(isSafeImage('https://app.test/api/img.png')).toBe(true);
    expect(isSafeImage('/relative/img.png')).toBe(true);
    expect(isSafeImage('https://tracker.example/pixel.gif')).toBe(false);
    expect(isSafeImage('data:image/svg+xml;base64,AAAA')).toBe(false);
    expect(isSafeImage(null)).toBe(false);
  });
});
