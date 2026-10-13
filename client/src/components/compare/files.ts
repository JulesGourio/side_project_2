import type { DocFile } from './types';

export const MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024;

export const PDF_MIME_TYPE = 'application/pdf';
export const DOCX_MIME_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

const MIME_BY_EXTENSION: Record<string, string> = {
  pdf: PDF_MIME_TYPE,
  docx: DOCX_MIME_TYPE,
  doc: 'application/msword',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  ppt: 'application/vnd.ms-powerpoint',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  xls: 'application/vnd.ms-excel',
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  gif: 'image/gif', webp: 'image/webp',
};

export const SUPPORTED_EXTENSIONS = new Set([
  'pdf',
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'tif',
  'docx', 'doc',
  'pptx', 'ppt',
  'xlsx', 'xls',
  'xml',
]);

export const FILE_ACCEPT = [
  PDF_MIME_TYPE,
  'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/tiff',
  DOCX_MIME_TYPE,
  'application/msword',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.ms-powerpoint',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel',
  ...Array.from(SUPPORTED_EXTENSIONS, ext => `.${ext}`),
].join(',');

export function extensionOf(name: string): string {
  return name.includes('.') ? name.split('.').pop()!.toLowerCase() : '';
}

export function mimeForName(name: string): string {
  return MIME_BY_EXTENSION[extensionOf(name)] || 'application/octet-stream';
}

export function baseName(name: string, fallback = 'document'): string {
  return name.replace(/\.[^.]+$/, '') || fallback;
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function b64ToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const bin = atob(b64);
  const bytes = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

export async function computeFileHash(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const buffer = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(buffer), b => b.toString(16).padStart(2, '0')).join('');
}

export function readFile(file: File): Promise<{ base64: string; bytes: Uint8Array<ArrayBuffer>; mimeType: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const base64 = (reader.result as string).split(',')[1];
      resolve({ base64, bytes: b64ToBytes(base64), mimeType: file.type || 'application/octet-stream' });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export function docBlob(doc: DocFile): Blob {
  return new Blob([doc.bytes], { type: doc.mimeType });
}

/** Name-only stand-in shown while a history entry's files are being restored. */
export function placeholderDoc(name: string): DocFile {
  return { name, size: 0, data: '', bytes: new Uint8Array(0) as Uint8Array<ArrayBuffer>, mimeType: mimeForName(name) };
}
