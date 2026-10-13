import { useState } from 'react';
import { LS, loadPdfFromStorage, lsGet, lsRemove, lsSet, savePdfToStorage } from '../storage';
import type { DocFile, DocSide } from '../types';

const LS_DOC: Record<DocSide, string> = { old: LS.oldPdf, new: LS.newPdf };

/** The two documents under comparison and their content hashes, mirrored to localStorage. */
export function useCompareDocs() {
  const [docs, setDocs] = useState<Record<DocSide, DocFile | null>>(() => ({
    old: loadPdfFromStorage(LS.oldPdf),
    new: loadPdfFromStorage(LS.newPdf),
  }));
  const [hashes, setHashesState] = useState({ old: lsGet(LS.oldHash) || '', new: lsGet(LS.newHash) || '' });

  /** `persist: false` shows a name-only placeholder without writing it to localStorage. */
  const setDoc = (side: DocSide, file: DocFile | null, persist = true) => {
    setDocs(prev => ({ ...prev, [side]: file }));
    if (persist) savePdfToStorage(LS_DOC[side], file);
  };

  const setHashes = (oldHash: string, newHash: string) => {
    setHashesState({ old: oldHash, new: newHash });
    lsSet(LS.oldHash, oldHash);
    lsSet(LS.newHash, newHash);
  };

  const clearHashes = () => {
    setHashesState({ old: '', new: '' });
    lsRemove(LS.oldHash, LS.newHash);
  };

  return { docs, hashes, setDoc, setHashes, clearHashes };
}
