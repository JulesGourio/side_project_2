import { useRef, useState } from 'react';
import { toast } from 'sonner';
import { downloadPdf } from '@/lib/exportPdf';
import { errorMessage } from '../api';
import { baseName, computeFileHash, docBlob } from '../files';
import { EMPTY_SUMMARY, type DocFile, type DocSide, type DocSummaryState } from '../types';

type Summaries = Record<DocSide, DocSummaryState>;

const SIDE_LABEL: Record<DocSide, string> = { old: 'Original (A)', new: 'Revised (B)' };

// Cached server-side by file hash, so re-summarizing the same file is instant unless "Re-run" is used.
export function useDocSummaries(docs: Record<DocSide, DocFile | null>) {
  const [summaries, setSummaries] = useState<Summaries>({ old: EMPTY_SUMMARY, new: EMPTY_SUMMARY });
  const [activeSide, setActiveSide] = useState<DocSide>('old');
  // Per side: the request that may still write its result (a new file or a re-run supersedes the previous one).
  const requestRef = useRef<Record<DocSide, number>>({ old: 0, new: 0 });

  const update = (side: DocSide, next: DocSummaryState | ((prev: DocSummaryState) => DocSummaryState)) =>
    setSummaries(prev => ({ ...prev, [side]: typeof next === 'function' ? next(prev[side]) : next }));

  const summarize = async (side: DocSide, forceRefresh = false) => {
    const doc = docs[side];
    if (!doc) return;
    setActiveSide(side);
    const request = ++requestRef.current[side];
    const isCurrent = () => request === requestRef.current[side];
    update(side, { ...EMPTY_SUMMARY, loading: true });
    try {
      const form = new FormData();
      form.append('file', docBlob(doc), doc.name);
      form.append('file_hash', await computeFileHash(doc.bytes));
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      const res = await fetch('/api/compare/summarize', { method: 'POST', body: form });
      const data = await res.json();
      if (!isCurrent()) return;
      if (!res.ok || data.error) throw new Error(data.error || `Server error: ${res.status}`);
      update(side, {
        text: data.summary || '',
        error: '',
        loading: false,
        noContent: !!data.no_content,
        meta: {
          truncated: !!data.truncated,
          durationS: data.duration_s ?? 0,
          inputTokens: data.usage?.input_tokens ?? 0,
          outputTokens: data.usage?.output_tokens ?? 0,
          totalTokens: data.usage?.total_tokens ?? 0,
          costEur: data.usage?.cost_eur ?? 0,
        },
      });
      if (data.cached) toast.info('Result retrieved from cache');
    } catch (err) {
      if (!isCurrent()) return;
      const message = errorMessage(err);
      update(side, prev => ({ ...prev, loading: false, error: message }));
      toast.error(`Summary failed: ${message}`);
    }
  };

  const download = async (side: DocSide) => {
    const doc = docs[side];
    const text = summaries[side].text;
    if (!doc || !text) return;
    const base = baseName(doc.name);
    try {
      await downloadPdf({
        kind: 'document_summary',
        title: base,
        filename: `summary_${base}`,
        meta: [{ label: 'File', value: doc.name }, { label: 'Version', value: SIDE_LABEL[side] }],
        sections: [{ markdown: text }],
      });
    } catch (e) {
      toast.error(`PDF export failed: ${errorMessage(e)}`);
    }
  };

  return {
    summaries,
    activeSide,
    setActiveSide,
    summarize,
    download,
    clear: (side?: DocSide) => {
      if (side) requestRef.current[side]++;
      else requestRef.current = { old: requestRef.current.old + 1, new: requestRef.current.new + 1 };
      if (side) update(side, EMPTY_SUMMARY);
      else setSummaries({ old: EMPTY_SUMMARY, new: EMPTY_SUMMARY });
    },
  };
}
