import { useRef } from 'react';
import { toast } from 'sonner';
import { loadSessionFiles } from '../api';
import { placeholderDoc } from '../files';
import type { FullComparison } from '../ComparisonHistory';
import type { ImpactResult } from '../impact/types';
import type { useAnalysisTrack } from './useAnalysisTrack';
import type { useCompareDocs } from './useCompareDocs';
import type { useImpactSearch } from './useImpactSearch';

type Track = ReturnType<typeof useAnalysisTrack>;

interface HistoryLoadContext {
  docs: ReturnType<typeof useCompareDocs>;
  structured: Track;
  standard: Track;
  impact: ReturnType<typeof useImpactSearch>;
  setError: (message: string) => void;
}

function parseSavedImpact(raw: string | null): ImpactResult | null {
  try {
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && Array.isArray(parsed.changes) && Array.isArray(parsed.documents) ? parsed : null;
  } catch {
    return null; // an entry saved without an impact result holds no structured data
  }
}

// A history row fills one track. The other track is kept only if it belongs to the same two files;
// otherwise it is reset, so it cannot describe another comparison next to these file names.
export function useHistoryLoad({ docs, structured, standard, impact, setError }: HistoryLoadContext) {
  const loadSeqRef = useRef(0);
  return async (entry: FullComparison) => {
    const seq = ++loadSeqRef.current;
    const isStructured = entry.processing_method === 'structured';
    const oldHash = entry.old_file_hash || '';
    const newHash = entry.new_file_hash || '';
    const samePair = !!oldHash && oldHash === docs.hashes.old && newHash === docs.hashes.new;

    setError('');
    impact.restore(parseSavedImpact(entry.impact_text), entry.impact_text || '');
    docs.setHashes(oldHash, newHash);
    if (!samePair) (isStructured ? standard : structured).reset();
    (isStructured ? structured : standard).restore(entry.analysis_text || '', entry.id);

    // Show the file names right away; the bytes come back from the volume if it still has them.
    docs.setDoc('old', placeholderDoc(entry.old_filename), false);
    docs.setDoc('new', placeholderDoc(entry.new_filename), false);

    const restored = entry.volume_session_path
      ? await loadSessionFiles(entry.volume_session_path, entry.old_filename, entry.new_filename)
      : {};
    // A later click owns the documents now.
    if (seq !== loadSeqRef.current) return;
    if (restored.old) docs.setDoc('old', restored.old);
    if (restored.new) docs.setDoc('new', restored.new);
    const arrow = `${entry.old_filename} → ${entry.new_filename}`;
    toast.success(`${restored.old || restored.new ? 'Restored' : 'Loaded'}: ${arrow}`);
  };
}
