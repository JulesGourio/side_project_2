import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { errorMessage, saveImpactToHistory } from '../api';
import type { ImpactResult } from '../impact/types';
import { LS, lsGetJson, lsRemove, lsSet } from '../storage';

interface ImpactSearchInput {
  changesText: string;
  oldHash: string;
  newHash: string;
  oldName: string;
  newName: string;
  /** The `messages` row to attach the result to, so reopening that history entry shows it again. */
  historyId: number | null;
}

/** Streams NDJSON from `/api/compare/impact`: plan, then one document per judged candidate, then done. */
async function streamImpact(res: Response, onUpdate: (result: ImpactResult) => void): Promise<ImpactResult | null> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let current: ImpactResult | null = null;
  const handleEvent = (ev: Record<string, unknown>) => {
    if (ev.type === 'error') throw new Error(String(ev.error));
    if (ev.type === 'plan') {
      current = { ...(ev as unknown as ImpactResult), documents: [], done: false };
    } else if (ev.type === 'document' && current) {
      current = { ...current, documents: [...current.documents, ev.document as ImpactResult['documents'][number]] };
    } else if (ev.type === 'done' && current) {
      current = {
        ...current, done: true, usage: ev.usage as ImpactResult['usage'], duration_s: ev.duration_s as number,
        impact_request_id: (ev.impact_request_id as number | null) ?? null,
      };
    }
    if (current) onUpdate(current);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) if (line.trim()) handleEvent(JSON.parse(line));
    if (done) break;
  }
  if (buffer.trim()) handleEvent(JSON.parse(buffer));
  return current;
}

export function useImpactSearch() {
  const [result, setResult] = useState<ImpactResult | null>(() => lsGetJson<ImpactResult>(LS.impact));
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const reset = () => {
    setResult(null);
    setError('');
    lsRemove(LS.impact);
  };

  const restore = (saved: ImpactResult | null, rawJson: string) => {
    setResult(saved);
    setError('');
    if (saved) lsSet(LS.impact, rawJson);
    else lsRemove(LS.impact);
  };

  const run = async (input: ImpactSearchInput, forceRefresh = false) => {
    if (!input.changesText) return;
    setRunning(true);
    reset();
    try {
      const controller = new AbortController();
      abortRef.current = controller;
      const res = await fetch('/api/compare/impact', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          changes_text: input.changesText,
          // Links the impact_requests row to this comparison; the server also uses these hashes as its cache key unless force_refresh.
          old_file_hash: input.oldHash,
          new_file_hash: input.newHash,
          // The compared document itself would be the top hit — the server drops it.
          old_file_name: input.oldName,
          new_file_name: input.newName,
          force_refresh: forceRefresh,
        }),
      });
      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `Server error: ${res.status}`);
      }
      const final = await streamImpact(res, setResult);
      if (!final?.done) throw new Error('The search was interrupted before it finished.');
      if (final.cached) toast.info('Result retrieved from cache');
      else if (final.no_changes) toast.info('Analysis reported no substantive changes — nothing to judge.');
      const json = JSON.stringify(final);
      lsSet(LS.impact, json);
      if (input.historyId !== null && !final.no_changes) saveImpactToHistory(input.historyId, json);
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        toast.info('Impact search cancelled.');
        return;
      }
      const message = errorMessage(err);
      setError(message);
      toast.error(`Impact search failed: ${message}`);
    } finally {
      setRunning(false);
    }
  };

  return { result, error, running, run, cancel: () => abortRef.current?.abort(), reset, restore };
}
