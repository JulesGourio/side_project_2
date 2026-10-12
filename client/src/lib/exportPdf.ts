/**
 * PDF exports (chatbot and compare) — rendered server-side by /api/export/pdf
 * (server/services/infra/pdf_report.py) so both share one layout and one font.
 * `kind` only sets the label shown above the title.
 */

export type PdfKind = 'chat_response' | 'chat_session' | 'change_summary' | 'document_summary';

export interface PdfSource {
  title: string;
  url?: string;
  n?: number;
  detail?: string;  // Intraqual title, revision and date, shown after the REF
}

export interface PdfSection {
  markdown: string;
  question?: string;
  sources?: PdfSource[];
}

export interface PdfExport {
  kind: PdfKind;
  title: string;
  filename: string;
  meta?: { label: string; value: string }[];
  sections: PdfSection[];
}

/** Fetch the PDF and save it; throws with the server's message on failure. */
export async function downloadPdf(req: PdfExport): Promise<void> {
  const res = await fetch('/api/export/pdf', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    throw new Error(err.error ?? res.statusText);
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = req.filename.endsWith('.pdf') ? req.filename : `${req.filename}.pdf`;
  a.click();
  URL.revokeObjectURL(url);
}
