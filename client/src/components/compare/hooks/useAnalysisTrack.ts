import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { STREAM_INTERRUPTED, autoSaveDocs, autoSaveResult, errorMessage, readAnalysisStream, saveToHistory } from '../api';
import { computeFileHash, docBlob } from '../files';
import { LS_STREAM_PERSIST_MS, lsGet, lsRemove, lsSet } from '../storage';
import type { DocFile, ImagePair } from '../types';

type ProcessorVersionId = 'structured' | 'standard';

interface SavedAnalysis {
  sessionPath: string;
  text: string;
  fileType: string;
  images: ImagePair[];
}

interface TrackOptions {
  version: ProcessorVersionId;
  label: string;
  lsText: string;
  lsMessageId: string;
  oldDoc: DocFile | null;
  newDoc: DocFile | null;
  volumeConfigured: boolean;
  /** Synchronous reset of whatever depends on the previous run (error banner, impact result…). */
  onBegin: () => void;
  onHashes: (oldHash: string, newHash: string) => void;
  onError: (message: string) => void;
  onFileType: (fileType: string) => void;
  onImages?: (images: ImagePair[]) => void;
  /** Derived artifacts (Excel, PDF) saved next to the session files after a fresh run. */
  onSaved: (saved: SavedAnalysis) => void;
}

function toUserMessage(err: unknown): string {
  const raw = errorMessage(err);
  const lower = raw.toLowerCase();
  const networkDrop = lower.includes('failed to fetch') || lower.includes('networkerror') || lower.includes('load failed');
  return networkDrop ? 'Connection lost during analysis. Please retry — the server may still be processing.' : raw;
}

/**
 * One streamed analysis of the two documents. The Change Table (`structured`) and
 * Change Summary (`standard`) tracks are two instances of this hook.
 */
export function useAnalysisTrack(opts: TrackOptions) {
  const [text, setText] = useState(() => lsGet(opts.lsText) || '');
  const [messageId, setMessageId] = useState<number | null>(() => {
    const stored = lsGet(opts.lsMessageId);
    return stored ? Number(stored) : null;
  });
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Abort the in-flight stream when the page unmounts (tab switch).
  useEffect(() => () => abortRef.current?.abort(), []);

  const cancel = useCallback(() => abortRef.current?.abort(), []);

  const reset = () => {
    setText('');
    setMessageId(null);
    lsRemove(opts.lsText, opts.lsMessageId);
  };

  const restore = (restoredText: string, id: number) => {
    setText(restoredText);
    setMessageId(id);
    lsSet(opts.lsText, restoredText);
    lsSet(opts.lsMessageId, String(id));
  };

  const run = async (forceRefresh = false) => {
    const { oldDoc, newDoc, version } = opts;
    if (!oldDoc || !newDoc) return;
    opts.onBegin();
    reset();
    setRunning(true);

    try {
      const [oldHash, newHash] = await Promise.all([computeFileHash(oldDoc.bytes), computeFileHash(newDoc.bytes)]);
      opts.onHashes(oldHash, newHash);
      const form = new FormData();
      form.append('old_file', docBlob(oldDoc), oldDoc.name);
      form.append('new_file', docBlob(newDoc), newDoc.name);
      form.append('old_file_hash', oldHash);
      form.append('new_file_hash', newHash);
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      form.append('processor_version', version);

      const controller = new AbortController();
      abortRef.current = controller;

      const startTime = Date.now();
      const res = await fetch('/api/compare/analyze', { method: 'POST', body: form, signal: controller.signal });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);

      let lastPersist = 0;
      let images: ImagePair[] = [];
      const out = await readAnalysisStream(res, {
        onText: accumulated => {
          setText(accumulated);
          if (Date.now() - lastPersist > LS_STREAM_PERSIST_MS) {
            lsSet(opts.lsText, accumulated);
            lastPersist = Date.now();
          }
        },
        onFileType: opts.onFileType,
        onImages: opts.onImages && (found => { images = found; opts.onImages!(found); }),
      });

      lsSet(opts.lsText, out.text);
      // A stream cut before its end marker leaves a partial report: keep it on screen, never cache it.
      if (!out.complete) throw new Error(STREAM_INTERRUPTED);

      if (out.cached) {
        setMessageId(out.cached.messageId);
        lsSet(opts.lsMessageId, String(out.cached.messageId));
        toast.info('Result retrieved from cache');
        return;
      }

      const endTime = Date.now();
      const ttftS = out.firstTokenAt !== null ? (out.firstTokenAt - startTime) / 1000 : 0;
      const generationS = (endTime - (out.firstTokenAt ?? startTime)) / 1000;
      // Session folders are only created for fresh (non-cached) analyses.
      const sessionPath = opts.volumeConfigured ? await autoSaveDocs(oldDoc, newDoc) : '';
      if (sessionPath) {
        await autoSaveResult(sessionPath, 'analysis.md', out.text);
        opts.onSaved({ sessionPath, text: out.text, fileType: out.fileType, images });
      }
      // Reports that carry a warning are saved without hashes: the cache replays only the text, so the warning would be lost.
      const id = await saveToHistory({
        oldName: oldDoc.name,
        newName: newDoc.name,
        analysisText: out.text,
        volumePath: sessionPath,
        oldHash: out.warned ? '' : oldHash,
        newHash: out.warned ? '' : newHash,
        fileType: out.fileType,
        processingMethod: version === 'standard' ? 'standard' : out.method,
        processorVersion: version,
        ttftS,
        generationS,
        usage: out.usage,
        llmRequestId: out.llmRequestId,
      });
      if (id !== null) {
        setMessageId(id);
        lsSet(opts.lsMessageId, String(id));
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        toast.info('Analysis cancelled — partial result kept.');
        return;
      }
      const message = toUserMessage(err);
      opts.onError(message);
      toast.error(`${opts.label} failed: ${message}`);
    } finally {
      setRunning(false);
    }
  };

  return { text, messageId, running, run, cancel, reset, restore };
}
