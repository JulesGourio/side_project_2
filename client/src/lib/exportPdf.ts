// Rendered server-side (server/services/infra/pdf_report/) so chat and compare share one layout and font.

type PdfKind = 'chat_response' | 'chat_session' | 'change_summary' | 'document_summary';

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

interface PdfExport {
  kind: PdfKind;
  title: string;
  filename: string;
  meta?: { label: string; value: string }[];
  sections: PdfSection[];
}

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
