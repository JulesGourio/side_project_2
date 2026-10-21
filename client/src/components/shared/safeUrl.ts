/** Hosts other than the app's own origin that Markdown images may load from. */
const ALLOWED_IMAGE_HOSTS: readonly string[] = [];

const INLINE_IMAGE_RE = /^data:image\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=]+$/i;

function parse(url: string): URL | null {
  try {
    return new URL(url, window.location.origin);
  } catch {
    return null;
  }
}

/** Whether a link target may be rendered as a clickable link: `https:` only. */
export function isSafeLink(href: string | undefined | null): href is string {
  if (!href) return false;
  const parsed = parse(href);
  return parsed !== null && parsed.protocol === 'https:';
}

/** Whether an image source may be loaded: an inline image, the app's own origin or an allow-listed `https:` host. */
export function isSafeImage(src: string | undefined | null): src is string {
  if (!src) return false;
  if (INLINE_IMAGE_RE.test(src)) return true;
  const parsed = parse(src);
  if (parsed === null) return false;
  if (parsed.origin === window.location.origin) return true;
  return parsed.protocol === 'https:' && ALLOWED_IMAGE_HOSTS.includes(parsed.host);
}
