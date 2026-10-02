/**
 * downloadAsPdf — styled PDF from Markdown using pure jsPDF text/drawing APIs.
 * No html2canvas, no DOM cloning, no blank-page issues.
 * Inline bold/italic markers are preserved and rendered per-span.
 */

import { jsPDF } from 'jspdf';

// ─── Page geometry (mm) ───────────────────────────────────────────────────────
const PW = 210;
const PH = 297;
const MX = 16;
const MY = 14;
const CW = PW - MX * 2;

// ─── Colour palette ──────────────────────────────────────────────────────────
const C = {
  blue:       [37, 99, 235]  as [number, number, number],
  blueDark:   [30, 58, 95]   as [number, number, number],
  blueMid:    [59, 130, 246] as [number, number, number],
  blueLight:  [239, 246, 255] as [number, number, number],
  slate900:   [15, 23, 42]   as [number, number, number],
  slate700:   [51, 65, 85]   as [number, number, number],
  slate500:   [100, 116, 139] as [number, number, number],
  slate300:   [203, 213, 225] as [number, number, number],
  slate200:   [226, 232, 240] as [number, number, number],
  slate100:   [241, 245, 249] as [number, number, number],
  slate50:    [248, 250, 252] as [number, number, number],
  white:      [255, 255, 255] as [number, number, number],
  codeText:   [30, 41, 59]   as [number, number, number],
  quoteText:  [30, 64, 175]  as [number, number, number],
  headerLabel: [140, 175, 230] as [number, number, number],
  headerSub:   [160, 195, 240] as [number, number, number],
};

// ─── Inline text processing ──────────────────────────────────────────────────

interface Span {
  text: string;
  bold: boolean;
  italic: boolean;
}

/**
 * Apply all cleanup (control chars, HTML entities, spaced-out chars, links,
 * strikethrough, inline code) WITHOUT removing bold/italic markers.
 * Used for paragraphs/bullets/ordered so span renderer can handle formatting.
 */
function cleanInline(s: string): string {
  return s
    .replace(/[\u00ad\u200b-\u200f\u2028\u2029\ufeff]/g, '')
    .replace(/\b(?:[A-Za-z] ){2,}[A-Za-z]\b/g, m => m.replace(/ /g, ''))
    .replace(/&amp;/g, '&').replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
    .replace(/~~(.+?)~~/g, '$1')
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

/**
 * Strip ALL inline markers → plain text.
 * Used for headings, table cells, blockquotes (uniform font rendering).
 */
function stripInline(s: string): string {
  return cleanInline(s)
    .replace(/\*\*\*(.+?)\*\*\*/g, '$1')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/\*([^*\n]+?)\*/g, '$1')
    .replace(/(^|\s)__(.+?)__(\s|$)/g, '$1$2$3');
}

/**
 * Parse inline text into typed spans preserving bold/italic.
 * Input must already be through cleanInline().
 */
function parseInlineSpans(s: string): Span[] {
  const spans: Span[] = [];
  // Match bold+italic ***, bold **, italic * (non-greedy, no newlines inside)
  const re = /\*\*\*([^*\n]+?)\*\*\*|\*\*([^*\n]+?)\*\*|\*([^*\n]+?)\*/g;
  let last = 0;
  let m: RegExpExecArray | null;

  while ((m = re.exec(s)) !== null) {
    if (m.index > last) {
      spans.push({ text: s.slice(last, m.index), bold: false, italic: false });
    }
    if (m[1] !== undefined) spans.push({ text: m[1], bold: true,  italic: true  }); // ***
    else if (m[2] !== undefined) spans.push({ text: m[2], bold: true,  italic: false }); // **
    else if (m[3] !== undefined) spans.push({ text: m[3], bold: false, italic: true  }); // *
    last = m.index + m[0].length;
  }
  if (last < s.length) {
    spans.push({ text: s.slice(last), bold: false, italic: false });
  }
  return spans.filter(sp => sp.text.length > 0);
}

// ─── Span-aware word-wrap ────────────────────────────────────────────────────

function measureW(doc: jsPDF, text: string, bold: boolean, italic: boolean, fontSize: number): number {
  const style = bold && italic ? 'bolditalic' : bold ? 'bold' : italic ? 'italic' : 'normal';
  doc.setFont('helvetica', style);
  doc.setFontSize(fontSize);
  return doc.getStringUnitWidth(text) * fontSize / doc.internal.scaleFactor;
}

interface Word { text: string; bold: boolean; italic: boolean }

function spansToWords(spans: Span[]): Word[] {
  const words: Word[] = [];
  for (const sp of spans) {
    const parts = sp.text.split(/(\s+)/);
    for (const p of parts) {
      if (p) words.push({ text: p, bold: sp.bold, italic: sp.italic });
    }
  }
  return words;
}

function mergeWords(words: Word[]): Span[] {
  const spans: Span[] = [];
  for (const w of words) {
    const last = spans[spans.length - 1];
    if (last && last.bold === w.bold && last.italic === w.italic) {
      last.text += w.text;
    } else {
      spans.push({ text: w.text, bold: w.bold, italic: w.italic });
    }
  }
  return spans;
}

function wrapSpanLines(doc: jsPDF, spans: Span[], maxW: number, fontSize: number): Span[][] {
  const words = spansToWords(spans);
  const lines: Span[][] = [];
  let lineWords: Word[] = [];
  let lineW = 0;

  for (const word of words) {
    const ww = measureW(doc, word.text, word.bold, word.italic, fontSize);
    if (lineW + ww > maxW && lineWords.length > 0 && word.text.trim() !== '') {
      lines.push(mergeWords(lineWords));
      lineWords = [word];
      lineW = ww;
    } else {
      lineWords.push(word);
      lineW += ww;
    }
  }
  if (lineWords.length > 0) lines.push(mergeWords(lineWords));
  return lines;
}

function renderSpanLine(st: St, spans: Span[], x: number, fontSize: number, color: [number, number, number]) {
  let cx = x;
  for (const sp of spans) {
    if (!sp.text) continue;
    const style = sp.bold && sp.italic ? 'bolditalic' : sp.bold ? 'bold' : sp.italic ? 'italic' : 'normal';
    st.doc.setFont('helvetica', style);
    st.doc.setFontSize(fontSize);
    rgb(st.doc, color, 'text');
    st.doc.text(sp.text, cx, st.y);
    cx += measureW(st.doc, sp.text, sp.bold, sp.italic, fontSize);
  }
}

// ─── Markdown tokeniser ───────────────────────────────────────────────────────

type Token =
  | { kind: 'heading';    level: number; text: string }
  | { kind: 'paragraph';  text: string }
  | { kind: 'bullet';     text: string; depth: number }
  | { kind: 'ordered';    text: string; num: number }
  | { kind: 'code';       lang: string; lines: string[] }
  | { kind: 'blockquote'; text: string }
  | { kind: 'hr' }
  | { kind: 'table';      headers: string[]; rows: string[][] };

function parseCells(row: string): string[] {
  return row.split('|').slice(1, -1).map(c => stripInline(c.trim()));
}

function tokenise(md: string): Token[] {
  const lines = md.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
  const tokens: Token[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trim = line.trim();

    // ── Fenced code block ──────────────────────────────────────────────────
    if (trim.startsWith('```')) {
      const lang = trim.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i]);
        i++;
      }
      i++;
      tokens.push({ kind: 'code', lang, lines: codeLines });
      continue;
    }

    // ── Heading — plain text (uniform bold rendering) ──────────────────────
    const hm = trim.match(/^(#{1,6})\s+(.+)$/);
    if (hm) {
      tokens.push({ kind: 'heading', level: hm[1].length, text: stripInline(hm[2]) });
      i++; continue;
    }

    // ── Horizontal rule ────────────────────────────────────────────────────
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trim)) {
      tokens.push({ kind: 'hr' });
      i++; continue;
    }

    // ── Blockquote — plain text (uniform bolditalic rendering) ────────────
    if (trim.startsWith('> ') || trim === '>') {
      const bLines: string[] = [];
      while (i < lines.length && (lines[i].trimStart().startsWith('> ') || lines[i].trim() === '>')) {
        bLines.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      tokens.push({ kind: 'blockquote', text: stripInline(bLines.join(' ')) });
      continue;
    }

    // ── Table — plain text cells ───────────────────────────────────────────
    if (trim.startsWith('|') && trim.endsWith('|') && i + 1 < lines.length && /^\|[-:| ]+\|$/.test(lines[i + 1]?.trim() ?? '')) {
      const headers = parseCells(trim);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        rows.push(parseCells(lines[i].trim()));
        i++;
      }
      tokens.push({ kind: 'table', headers, rows });
      continue;
    }

    // ── Unordered list — preserve inline markers for span renderer ─────────
    if (/^\s*[-*+]\s/.test(line)) {
      const depth = Math.floor((line.match(/^(\s*)/)?.[1]?.length ?? 0) / 2);
      tokens.push({ kind: 'bullet', text: cleanInline(line.replace(/^\s*[-*+]\s/, '')), depth });
      i++; continue;
    }

    // ── Ordered list — preserve inline markers ────────────────────────────
    const om = line.match(/^\s*(\d+)\.\s(.+)$/);
    if (om) {
      tokens.push({ kind: 'ordered', text: cleanInline(om[2]), num: parseInt(om[1]) });
      i++; continue;
    }

    // ── Empty line ─────────────────────────────────────────────────────────
    if (!trim) { i++; continue; }

    // ── Paragraph — preserve inline markers ───────────────────────────────
    const pLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].trim().startsWith('#') &&
      !lines[i].trim().startsWith('```') &&
      !lines[i].trimStart().startsWith('> ') &&
      !lines[i].trim().startsWith('|') &&
      !/^\s*[-*+]\s/.test(lines[i]) &&
      !/^\s*\d+\.\s/.test(lines[i]) &&
      !/^(-{3,}|\*{3,}|_{3,})$/.test(lines[i].trim())
    ) {
      pLines.push(lines[i]);
      i++;
    }
    if (pLines.length) {
      tokens.push({ kind: 'paragraph', text: cleanInline(pLines.join(' ')) });
    }
  }
  return tokens;
}

// ─── jsPDF renderer ──────────────────────────────────────────────────────────

function rgb(doc: jsPDF, color: [number, number, number], target: 'text' | 'fill' | 'draw') {
  if (target === 'text') doc.setTextColor(color[0], color[1], color[2]);
  if (target === 'fill') doc.setFillColor(color[0], color[1], color[2]);
  if (target === 'draw') doc.setDrawColor(color[0], color[1], color[2]);
}

interface St { doc: jsPDF; y: number }

function newPage(st: St) { st.doc.addPage(); st.y = MY; }
function ensure(st: St, need: number) { if (st.y + need > PH - MY) newPage(st); }

function wrap(doc: jsPDF, text: string, maxW: number): string[] {
  return doc.splitTextToSize(text, maxW) as string[];
}

function renderHeading(st: St, level: number, text: string) {
  const cfg: Record<number, { size: number; topPad: number; gap: number; bar: boolean }> = {
    1: { size: 16, topPad: 6, gap: 5, bar: true  },
    2: { size: 13, topPad: 5, gap: 4, bar: true  },
    3: { size: 11, topPad: 4, gap: 3, bar: false },
    4: { size: 10, topPad: 3, gap: 2, bar: false },
    5: { size: 9,  topPad: 3, gap: 2, bar: false },
    6: { size: 8,  topPad: 2, gap: 2, bar: false },
  };
  const { size, topPad, gap, bar } = cfg[level] ?? cfg[4];
  ensure(st, topPad + size * 0.5 + gap + 3);
  st.y += topPad;
  st.doc.setFont('helvetica', 'bold');
  st.doc.setFontSize(size);
  rgb(st.doc, level <= 2 ? C.slate900 : C.slate700, 'text');
  const lines = wrap(st.doc, text, CW);
  for (const ln of lines) {
    ensure(st, size * 0.5 + 2);
    st.doc.text(ln, MX, st.y);
    st.y += size * 0.45;
  }
  if (bar) {
    st.y += 1.5;
    rgb(st.doc, level === 1 ? C.blue : C.slate200, 'fill');
    st.doc.rect(MX, st.y, CW, level === 1 ? 0.7 : 0.35, 'F');
    st.y += 1;
  }
  st.y += gap;
}

function renderParagraph(st: St, text: string) {
  const fontSize = 10;
  const lineH = 5;
  const spans = parseInlineSpans(text);
  const lines = wrapSpanLines(st.doc, spans, CW, fontSize);
  for (const line of lines) {
    ensure(st, lineH + 0.5);
    renderSpanLine(st, line, MX, fontSize, C.slate700);
    st.y += lineH;
  }
  st.y += 2;
}

function renderBullet(st: St, text: string, depth: number) {
  const indent = MX + depth * 5 + 4;
  const maxW = CW - depth * 5 - 7;
  const fontSize = 10;
  const lineH = 4.8;

  const spans = parseInlineSpans(text);
  const lines = wrapSpanLines(st.doc, spans, maxW, fontSize);

  ensure(st, 6);
  rgb(st.doc, C.blue, 'fill');
  st.doc.circle(indent - 2.8, st.y - 1.5, 0.9, 'F');

  for (let li = 0; li < lines.length; li++) {
    ensure(st, lineH);
    renderSpanLine(st, lines[li], indent, fontSize, C.slate700);
    st.y += lineH;
  }
}

function renderOrdered(st: St, text: string, num: number) {
  const fontSize = 10;
  const lineH = 4.8;

  ensure(st, 6);
  st.doc.setFont('helvetica', 'bold');
  st.doc.setFontSize(fontSize);
  rgb(st.doc, C.blue, 'text');
  st.doc.text(`${num}.`, MX, st.y);

  const spans = parseInlineSpans(text);
  const lines = wrapSpanLines(st.doc, spans, CW - 8, fontSize);
  for (let li = 0; li < lines.length; li++) {
    ensure(st, lineH);
    renderSpanLine(st, lines[li], MX + 7, fontSize, C.slate700);
    st.y += lineH;
  }
}

function renderCode(st: St, lang: string, codeLines: string[]) {
  const lineH = 4;
  const padV = 3;
  const labelH = lang ? 5 : 0;
  const totalH = labelH + padV + codeLines.length * lineH + padV;

  ensure(st, Math.min(totalH + 4, PH * 0.35));
  st.y += 2;

  const blockH = Math.min(totalH, PH - st.y - MY);
  rgb(st.doc, C.slate100, 'fill');
  rgb(st.doc, C.slate200, 'draw');
  st.doc.setLineWidth(0.2);
  st.doc.roundedRect(MX, st.y, CW, blockH, 1.5, 1.5, 'FD');

  if (lang) {
    st.doc.setFont('courier', 'bold');
    st.doc.setFontSize(7.5);
    rgb(st.doc, C.slate500, 'text');
    st.doc.text(lang.toUpperCase(), MX + 3, st.y + 3.5);
    st.y += labelH;
  }

  st.y += padV;
  st.doc.setFont('courier', 'normal');
  st.doc.setFontSize(8.5);
  rgb(st.doc, C.codeText, 'text');

  for (const cl of codeLines) {
    if (st.y > PH - MY - lineH) {
      newPage(st);
      const rem = Math.min(codeLines.length * lineH + padV * 2, PH - st.y - MY);
      rgb(st.doc, C.slate100, 'fill');
      rgb(st.doc, C.slate200, 'draw');
      st.doc.roundedRect(MX, st.y, CW, rem, 1.5, 1.5, 'FD');
      st.y += padV;
    }
    const truncated = cl.length > 105 ? cl.slice(0, 102) + '...' : cl;
    st.doc.text(truncated, MX + 3, st.y);
    st.y += lineH;
  }
  st.y += padV + 3;
}

function renderBlockquote(st: St, text: string) {
  const lines = st.doc.splitTextToSize(text, CW - 10) as string[];
  const bH = lines.length * 5 + 5;
  ensure(st, bH + 4);
  st.y += 2;
  rgb(st.doc, C.blueLight, 'fill');
  st.doc.rect(MX, st.y - 1, CW, bH, 'F');
  rgb(st.doc, C.blue, 'fill');
  st.doc.rect(MX, st.y - 1, 1.2, bH, 'F');
  st.doc.setFont('helvetica', 'bolditalic');
  st.doc.setFontSize(10);
  rgb(st.doc, C.quoteText, 'text');
  for (const ln of lines) {
    st.doc.text(ln, MX + 5, st.y + 2.5);
    st.y += 5;
  }
  st.y += 4;
}

function renderHr(st: St) {
  ensure(st, 6);
  st.y += 3;
  rgb(st.doc, C.slate200, 'draw');
  st.doc.setLineWidth(0.3);
  st.doc.line(MX, st.y, MX + CW, st.y);
  st.y += 4;
}

function renderTable(st: St, headers: string[], rows: string[][]) {
  const cols = Math.max(headers.length, 1);
  const colW = CW / cols;
  const cellH = 6.5;
  const headerH = 7.5;
  const totalH = headerH + rows.length * cellH;

  ensure(st, Math.min(totalH + 4, PH * 0.4));
  st.y += 2;

  rgb(st.doc, C.blueDark, 'fill');
  st.doc.rect(MX, st.y, CW, headerH, 'F');
  st.doc.setFont('helvetica', 'bold');
  st.doc.setFontSize(8);
  rgb(st.doc, C.white, 'text');
  for (let ci = 0; ci < headers.length; ci++) {
    st.doc.text(headers[ci].slice(0, 30), MX + ci * colW + 2, st.y + 5);
  }
  st.y += headerH;

  for (let ri = 0; ri < rows.length; ri++) {
    ensure(st, cellH + 1);
    rgb(st.doc, ri % 2 === 0 ? C.white : C.slate50, 'fill');
    rgb(st.doc, C.slate200, 'draw');
    st.doc.setLineWidth(0.15);
    st.doc.rect(MX, st.y, CW, cellH, 'FD');
    st.doc.setFont('helvetica', 'normal');
    st.doc.setFontSize(9);
    rgb(st.doc, C.slate700, 'text');
    for (let ci = 0; ci < rows[ri].length; ci++) {
      st.doc.text(rows[ri][ci].slice(0, 32), MX + ci * colW + 2, st.y + 4.2);
    }
    st.y += cellH;
  }
  st.y += 4;
}

function renderFooter(doc: jsPDF, fileName: string, page: number, total: number) {
  const fy = PH - 8;
  rgb(doc, C.slate200, 'draw');
  doc.setLineWidth(0.2);
  doc.line(MX, fy - 1, MX + CW, fy - 1);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(7.5);
  rgb(doc, C.slate500, 'text');
  doc.text('Qualibot', MX, fy + 2);
  doc.text(fileName, MX + CW / 2, fy + 2, { align: 'center' });
  doc.text(`${page} / ${total}`, MX + CW, fy + 2, { align: 'right' });
}

// ─── Internal builder ─────────────────────────────────────────────────────────

function _buildDoc(title: string, content: string, fileName: string): jsPDF {
  const doc = new jsPDF({ unit: 'mm', format: 'a4', orientation: 'portrait' });
  const dateStr = new Date().toLocaleDateString('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric',
  });

  // ── Cover header ──────────────────────────────────────────────────────────
  const headerH = 28;
  rgb(doc, C.blueDark, 'fill');
  doc.rect(0, 0, PW, headerH, 'F');
  rgb(doc, C.blueMid, 'fill');
  doc.rect(PW * 0.45, 0, PW * 0.55, headerH, 'F');

  doc.setFont('helvetica', 'bold');
  doc.setFontSize(7.5);
  rgb(doc, C.headerLabel, 'text');
  doc.text('COMPARISON REPORT', MX, 9);

  doc.setFont('helvetica', 'bold');
  doc.setFontSize(15);
  rgb(doc, C.white, 'text');
  const titleLines = wrap(doc, title, CW - 5).slice(0, 2);
  let ty = 17;
  for (const tl of titleLines) {
    doc.text(tl, MX, ty);
    ty += 6;
  }

  doc.setFont('helvetica', 'normal');
  doc.setFontSize(8);
  rgb(doc, C.headerSub, 'text');
  doc.text(`Generated on ${dateStr}`, MX, 26);

  // ── Body ─────────────────────────────────────────────────────────────────
  const st: St = { doc, y: headerH + MY };
  const tokens = tokenise(content);
  for (let ti = 0; ti < tokens.length; ti++) {
    const tok = tokens[ti];
    if (tok.kind === 'hr') {
      const prev = ti > 0 ? tokens[ti - 1] : null;
      if (prev?.kind === 'heading') continue;
    }
    switch (tok.kind) {
      case 'heading':    renderHeading(st, tok.level, tok.text); break;
      case 'paragraph':  renderParagraph(st, tok.text); break;
      case 'bullet':     renderBullet(st, tok.text, tok.depth); break;
      case 'ordered':    renderOrdered(st, tok.text, tok.num); break;
      case 'code':       renderCode(st, tok.lang, tok.lines); break;
      case 'blockquote': renderBlockquote(st, tok.text); break;
      case 'hr':         renderHr(st); break;
      case 'table':      renderTable(st, tok.headers, tok.rows); break;
    }
  }

  // ── Page footers ─────────────────────────────────────────────────────────
  const total = (doc.internal as any).getNumberOfPages() as number;
  for (let p = 1; p <= total; p++) {
    doc.setPage(p);
    renderFooter(doc, fileName, p, total);
  }

  return doc;
}

// ─── Public API ───────────────────────────────────────────────────────────────

export function generatePdfBlob(title: string, content: string, fileName: string): Blob {
  if (!content.trim()) return new Blob([], { type: 'application/pdf' });
  return _buildDoc(title, content, fileName).output('blob') as Blob;
}

export function downloadAsPdf(title: string, content: string, fileName: string): void {
  if (!content.trim()) return;
  _buildDoc(title, content, fileName).save(fileName);
}
