import { useEffect, useRef, useState } from 'react';
import { AlertCircle, ArrowRight, Database, FileText, History, Info, Loader2, RotateCcw, Square, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { type ProcessorVersion, getAppConfig, getProcessorsConfig } from '@/lib/config';
import { ComparisonHistory, type FullComparison } from './ComparisonHistory';
import { WhatsNewButton, WhatsNewModal } from '@/components/layout/WhatsNewBanner';
import { type ImpactResult, ImpactResultsCard } from './ImpactResults';
import { DiffItem, DocFile, DocSummaryState, EMPTY_SUMMARY, IMPACT_MANUAL_MODE_ENABLED, ImagePair, LS_ANALYSIS, LS_ANALYSIS_STD, LS_IMPACT, LS_IMPACT_MANUAL_TEXT, LS_IMPACT_MODE, LS_MESSAGE_ID, LS_MESSAGE_ID_STD, LS_NEW_HASH, LS_NEW_PDF, LS_OLD_HASH, LS_OLD_PDF, LS_PROCESSING_METHOD, LS_SESSION_PATH, LS_STREAM_PERSIST_MS, STREAM_INTERRUPTED, computeFileHash, loadPdfFromStorage, lsSet, savePdfToStorage } from './compareShared';
import { PdfDropZone } from './PdfDropZone';
import { parsePartialJsonItems } from './JsonDiffTable';
import { StructuredResultCard } from './StructuredResultCard';
import { ResultCard } from './ResultCard';
import { DocSummaryCard } from './DocSummaryCard';

export function CompareView() {
  const [oldPdf, setOldPdf] = useState<DocFile | null>(null);
  const [newPdf, setNewPdf] = useState<DocFile | null>(null);
  const [analysis, setAnalysis] = useState('');
  const [impact, setImpact] = useState<ImpactResult | null>(null);
  const [impactError, setImpactError] = useState('');
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isImpacting, setIsImpacting] = useState(false);
  const [_sessionPath, setSessionPath] = useState('');
  const [error, setError] = useState('');
  const [volumeConfigured, setVolumeConfigured] = useState(false);
  const [impactConfigured, setImpactConfigured] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showWhatsNew, setShowWhatsNew] = useState(false);
  const [messageId, setMessageId] = useState<number | null>(null);
  const [oldFileHash, setOldFileHash] = useState('');
  const [newFileHash, setNewFileHash] = useState('');
  const [currentFileType, setCurrentFileType] = useState('');
  const [parsedJsonItems, setParsedJsonItems] = useState<DiffItem[]>([]);
  const [imageContext, setImageContext] = useState<ImagePair[]>([]);
  // Change Summary (standard) track — runs alongside Change Table (structured), no selector
  const [analysisStd, setAnalysisStd] = useState('');
  const [isAnalyzingStd, setIsAnalyzingStd] = useState(false);
  const [messageIdStd, setMessageIdStd] = useState<number | null>(null);
  // Which processor versions the current file type actually exposes (from processors.yml)
  const [availableVersions, setAvailableVersions] = useState<ProcessorVersion[]>([]);
  // Single-document summary — independent of the diff, keyed by which file
  const [summaries, setSummaries] = useState<{ old: DocSummaryState; new: DocSummaryState }>({ old: EMPTY_SUMMARY, new: EMPTY_SUMMARY });
  // Which summary tab is shown when both A and B are active — always full-width, one at a time.
  const [activeSummaryTab, setActiveSummaryTab] = useState<'old' | 'new'>('old');
  // Which main-analysis tab is shown when both Change Summary and Change Table are active —
  // same reasoning as activeSummaryTab: side-by-side halves each card's width and reads as cramped.
  const [activeAnalysisTab, setActiveAnalysisTab] = useState<'summary' | 'table'>('summary');
  // Impact search can run off a Compare analysis, or off a hand-typed change description
  // (an email, a general idea) when the user doesn't have both documents on hand.
  const [impactMode, setImpactMode] = useState<'compare' | 'manual'>(
    () => (localStorage.getItem(LS_IMPACT_MODE) === 'manual' ? 'manual' : 'compare'),
  );
  const [manualChangesText, setManualChangesText] = useState(() => localStorage.getItem(LS_IMPACT_MANUAL_TEXT) || '');
  // Manual mode's UI is hidden (IMPACT_MANUAL_MODE_ENABLED) — force 'compare' everywhere it's read,
  // even if a stale 'manual' value lingers in localStorage from before the toggle was hidden.
  const effectiveImpactMode: 'compare' | 'manual' = IMPACT_MANUAL_MODE_ENABLED ? impactMode : 'compare';

  // Abort in-flight analysis when component unmounts (tab switch)
  const abortControllerRef = useRef<AbortController | null>(null);
  const abortControllerRefStd = useRef<AbortController | null>(null);
  const abortControllerRefImpact = useRef<AbortController | null>(null);
  useEffect(() => () => {
    abortControllerRef.current?.abort();
    abortControllerRefStd.current?.abort();
    abortControllerRefImpact.current?.abort();
  }, []);

  useEffect(() => {
    const old = loadPdfFromStorage(LS_OLD_PDF);
    const nw = loadPdfFromStorage(LS_NEW_PDF);
    if (old) setOldPdf(old);
    if (nw) setNewPdf(nw);
    const storedAnalysis = localStorage.getItem(LS_ANALYSIS) || '';
    setAnalysis(storedAnalysis);
    setAnalysisStd(localStorage.getItem(LS_ANALYSIS_STD) || '');
    const storedMidStd = localStorage.getItem(LS_MESSAGE_ID_STD);
    if (storedMidStd) setMessageIdStd(Number(storedMidStd));
    try {
      const storedImpact = localStorage.getItem(LS_IMPACT);
      if (storedImpact) setImpact(JSON.parse(storedImpact) as ImpactResult);
    } catch { /* ignore malformed cache */ }
    setSessionPath(localStorage.getItem(LS_SESSION_PATH) || '');
    const storedMid = localStorage.getItem(LS_MESSAGE_ID);
    if (storedMid) setMessageId(Number(storedMid));
    setOldFileHash(localStorage.getItem(LS_OLD_HASH) || '');
    setNewFileHash(localStorage.getItem(LS_NEW_HASH) || '');
    if (storedAnalysis) {
      setParsedJsonItems(parsePartialJsonItems(storedAnalysis));
    }
  }, []);

  useEffect(() => {
    getAppConfig().then((cfg) => {
      const vp = cfg.compare?.volume_path || '';
      setVolumeConfigured(!!vp && !vp.startsWith('TODO'));
      const idx = (cfg.compare?.impact_index || '').trim();
      // A real Vector Search index name is alphanumeric + dots/hyphens/underscores, no spaces.
      // Treat anything else (empty, TODO, --, -, contains spaces, too short) as not configured.
      const looksReal = idx.length >= 3 && !idx.startsWith('TODO') && !/\s/.test(idx) && !/^-+$/.test(idx);
      setImpactConfigured(looksReal);
    });
  }, []);

  // Load available processor versions whenever the primary file changes.
  // File type is resolved from processors.yml extensions — processors.yml is the single source of truth.
  useEffect(() => {
    const filename = oldPdf?.name || newPdf?.name;
    if (!filename) {
      setAvailableVersions([]);
      return;
    }
    const ext = filename.includes('.') ? '.' + filename.split('.').pop()!.toLowerCase() : '';
    getProcessorsConfig().then((cfg) => {
      const entry = Object.values(cfg.file_types ?? {}).find(ft =>
        (ft.extensions ?? []).includes(ext)
      );
      const versions: ProcessorVersion[] = (entry?.versions ?? []).filter(v => !v.hidden);
      setAvailableVersions(versions);
    });
  }, [oldPdf?.name, newPdf?.name]);

  const clearAnalysisState = () => {
    setAnalysis('');
    setAnalysisStd('');
    setImpact(null);
    setImpactError('');
    setParsedJsonItems([]);
    setMessageId(null);
    setMessageIdStd(null);
    setError('');
    localStorage.removeItem(LS_ANALYSIS);
    localStorage.removeItem(LS_ANALYSIS_STD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_PROCESSING_METHOD);
    localStorage.removeItem(LS_MESSAGE_ID);
    localStorage.removeItem(LS_MESSAGE_ID_STD);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);
  };

  const setOldPdfAndPersist = (f: DocFile | null) => { setOldPdf(f); savePdfToStorage(LS_OLD_PDF, f); };
  const setNewPdfAndPersist = (f: DocFile | null) => { setNewPdf(f); savePdfToStorage(LS_NEW_PDF, f); };

  const setManualChangesTextAndPersist = (text: string) => {
    setManualChangesText(text);
    lsSet(LS_IMPACT_MANUAL_TEXT, text);
  };

  const switchImpactMode = (mode: 'compare' | 'manual') => {
    setImpactMode(mode);
    lsSet(LS_IMPACT_MODE, mode);
    setImpact(null);
    setImpactError('');
    localStorage.removeItem(LS_IMPACT);
  };

  const clearAll = () => {
    setOldPdfAndPersist(null);
    setNewPdfAndPersist(null);
    setAnalysis('');
    setAnalysisStd('');
    setImpact(null);
    setImpactError('');
    setSummaries({ old: EMPTY_SUMMARY, new: EMPTY_SUMMARY });
    setSessionPath('');
    setMessageId(null);
    setMessageIdStd(null);
    setOldFileHash('');
    setNewFileHash('');
    setError('');
    setManualChangesTextAndPersist('');
    localStorage.removeItem(LS_ANALYSIS);
    localStorage.removeItem(LS_ANALYSIS_STD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_SESSION_PATH);
    localStorage.removeItem(LS_MESSAGE_ID);
    localStorage.removeItem(LS_MESSAGE_ID_STD);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);
  };

  const handleInvalidFile = (msg: string) => { toast.error(msg); setError(msg); };

  // --- Auto-save helpers ---

  const autoSavePdfs = async (old: DocFile, nw: DocFile): Promise<string> => {
    try {
      const form = new FormData();
      form.append('old_file', new Blob([old.bytes], { type: old.mimeType }), old.name);
      form.append('new_file', new Blob([nw.bytes], { type: nw.mimeType }), nw.name);
      const res = await fetch('/api/compare/save', { method: 'POST', body: form });
      const data = await res.json();
      if (data.session_path) { lsSet(LS_SESSION_PATH, data.session_path); return data.session_path; }
      if (data.error) toast.warning(`Auto-save skipped: ${data.error}`);
    } catch (e) {
      toast.warning(`Auto-save unavailable: ${e instanceof Error ? e.message : String(e)}`);
    }
    return '';
  };

  const autoSaveResult = async (path: string, filename: string, content: string) => {
    if (!path || !content.trim()) return;
    try {
      const form = new FormData();
      form.append('session_path', path);
      form.append('filename', filename);
      form.append('content', content);
      const res = await fetch('/api/compare/save-result', { method: 'POST', body: form });
      const data = await res.json();
      if (data.error) toast.warning(`Could not save ${filename}: ${data.error}`);
    } catch (e) {
      toast.warning(`Could not save ${filename}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const autoSaveExcel = (path: string, jsonText: string, fileType: string, imagePairs?: ImagePair[]) => {
    if (!path || !jsonText.trim()) return;
    const form = new FormData();
    form.append('session_path', path);
    form.append('json_text', jsonText);
    form.append('file_type', fileType);
    form.append('filename', 'analysis');
    if (imagePairs && imagePairs.length > 0) {
      form.append('image_pairs_json', JSON.stringify(imagePairs));
    }
    fetch('/api/compare/save-excel', { method: 'POST', body: form })
      .then(r => r.json())
      .then(d => { if (d.error) toast.warning(`Could not save Excel: ${d.error}`); })
      .catch(() => { /* best-effort — not critical */ });
  };

  const autoSavePdf = (path: string, markdownText: string, title: string) => {
    if (!path || !markdownText.trim()) return;
    const form = new FormData();
    form.append('session_path', path);
    form.append('markdown_text', markdownText);
    form.append('title', title);
    form.append('filename', 'analysis');
    fetch('/api/compare/save-pdf', { method: 'POST', body: form })
      .then(r => r.json())
      .then(d => { if (d.error) toast.warning(`Could not save PDF: ${d.error}`); })
      .catch(() => { /* best-effort — not critical */ });
  };

  // --- History helpers ---

  const saveToHistory = async (
    oldName: string,
    newName: string,
    analysisText: string,
    impactText: string,
    volumePath: string,
    oldHash = '',
    newHash = '',
    fileType = '',
    processingMethod = '',
    processorVersion = '',
    ttftS = 0,
    generationS = 0,
    usage: { input_tokens?: number; output_tokens?: number; thinking_tokens?: number; total_tokens?: number; cost_eur?: number } = {},
    llmRequestId: number | null = null,
  ): Promise<number | null> => {
    try {
      const res = await fetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_filename: oldName,
          new_filename: newName,
          old_file_hash: oldHash || null,
          new_file_hash: newHash || null,
          analysis_text: analysisText || null,
          impact_text: impactText || null,
          volume_session_path: volumePath || null,
          file_type: fileType || null,
          processing_method: processingMethod || null,
          processor_version: processorVersion || null,
          ttft_s: ttftS || null,
          generation_s: generationS || null,
          input_tokens: usage.input_tokens ?? null,
          output_tokens: usage.output_tokens ?? null,
          thinking_tokens: usage.thinking_tokens ?? null,
          total_tokens: usage.total_tokens ?? null,
          cost_eur: usage.cost_eur ?? null,
          llm_request_id: llmRequestId ?? null,
        }),
      });
      const data = await res.json();
      return typeof data.messages_id === 'number' ? data.messages_id : null;
    } catch {
      return null;
    }
  };

  const handleLoadFromHistory = async (entry: FullComparison) => {
    // A history row is one run of one processor version — route it into the
    // matching track (structured = Change Table, everything else = Change
    // Summary) and leave the other track untouched.
    const method = entry.processing_method || '';
    // The other track and the impact results stay only if they belong to the
    // same two files; otherwise they would sit next to this entry's file names
    // (and feed the next impact search) while describing another comparison.
    const entryOldHash = entry.old_file_hash || '';
    const entryNewHash = entry.new_file_hash || '';
    const samePair = !!entryOldHash && entryOldHash === oldFileHash && entryNewHash === newFileHash;
    setError('');
    // The impact result saved with this entry, if a search was run on it.
    let savedImpact: ImpactResult | null = null;
    try {
      const parsed = entry.impact_text ? JSON.parse(entry.impact_text) : null;
      if (parsed && Array.isArray(parsed.changes) && Array.isArray(parsed.documents)) savedImpact = parsed;
    } catch { /* older entries hold plain text or nothing */ }
    setImpact(savedImpact);
    setImpactError('');
    if (savedImpact) lsSet(LS_IMPACT, entry.impact_text || '');
    else localStorage.removeItem(LS_IMPACT);
    setOldFileHash(entryOldHash);
    setNewFileHash(entryNewHash);
    lsSet(LS_OLD_HASH, entryOldHash);
    lsSet(LS_NEW_HASH, entryNewHash);
    setSessionPath(entry.volume_session_path || '');
    lsSet(LS_SESSION_PATH, entry.volume_session_path || '');
    if (!samePair) {
      if (method === 'structured') {
        setAnalysisStd('');
        setMessageIdStd(null);
        localStorage.removeItem(LS_ANALYSIS_STD);
        localStorage.removeItem(LS_MESSAGE_ID_STD);
      } else {
        setAnalysis('');
        setParsedJsonItems([]);
        setMessageId(null);
        localStorage.removeItem(LS_ANALYSIS);
        localStorage.removeItem(LS_MESSAGE_ID);
      }
    }

    if (method === 'structured') {
      setParsedJsonItems(entry.analysis_text ? parsePartialJsonItems(entry.analysis_text) : []);
      setAnalysis(entry.analysis_text || '');
      setMessageId(entry.id);
      lsSet(LS_PROCESSING_METHOD, method);
      lsSet(LS_ANALYSIS, entry.analysis_text || '');
      lsSet(LS_MESSAGE_ID, String(entry.id));
    } else {
      setAnalysisStd(entry.analysis_text || '');
      setMessageIdStd(entry.id);
      lsSet(LS_ANALYSIS_STD, entry.analysis_text || '');
      lsSet(LS_MESSAGE_ID_STD, String(entry.id));
    }

    // Show filename placeholders immediately (no bytes — display only, not persisted to localStorage)
    const emptyBytes = new Uint8Array(0) as unknown as Uint8Array<ArrayBuffer>;
    const mimeFor = (name: string) => {
      const ext = name.split('.').pop()?.toLowerCase() || '';
      const m: Record<string, string> = {
        pdf: 'application/pdf',
        docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        doc: 'application/msword',
        pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        ppt: 'application/vnd.ms-powerpoint',
        xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        xls: 'application/vnd.ms-excel',
        jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
        gif: 'image/gif', webp: 'image/webp',
      };
      return m[ext] || 'application/octet-stream';
    };
    setOldPdf({ name: entry.old_filename, size: 0, data: '', bytes: emptyBytes, mimeType: mimeFor(entry.old_filename) });
    setNewPdf({ name: entry.new_filename, size: 0, data: '', bytes: emptyBytes, mimeType: mimeFor(entry.new_filename) });

    // Try to restore actual file bytes from volume
    if (entry.volume_session_path) {
      try {
        const params = new URLSearchParams({
          session_path: entry.volume_session_path,
          old_filename: entry.old_filename,
          new_filename: entry.new_filename,
        });
        const res = await fetch(`/api/compare/load?${params}`);
        if (res.ok) {
          const data = await res.json();
          const b64toBytes = (b64: string): Uint8Array<ArrayBuffer> => {
            const bin = atob(b64);
            const buf = new ArrayBuffer(bin.length);
            const bytes = new Uint8Array(buf);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            return bytes;
          };
          if (data.old) {
            const f: DocFile = { name: data.old.name, size: data.old.size, data: data.old.data, bytes: b64toBytes(data.old.data), mimeType: data.old.mimeType };
            setOldPdfAndPersist(f);
          }
          if (data.new) {
            const f: DocFile = { name: data.new.name, size: data.new.size, data: data.new.data, bytes: b64toBytes(data.new.data), mimeType: data.new.mimeType };
            setNewPdfAndPersist(f);
          }
          if (data.old || data.new) {
            toast.success(`Restored: ${entry.old_filename} → ${entry.new_filename}`);
            return;
          }
        }
      } catch {
        // best-effort — fall through
      }
    }
    toast.success(`Loaded: ${entry.old_filename} → ${entry.new_filename}`);
  };

  // --- Analyze ---

  const handleAnalyzeStructured = async (forceRefresh = false) => {
    if (!oldPdf || !newPdf) return;
    setError('');
    setAnalysis('');
    setImpact(null);
    setImpactError('');
    setSessionPath('');
    setMessageId(null);
    setParsedJsonItems([]);
    setImageContext([]);
    setIsAnalyzing(true);
    localStorage.removeItem(LS_ANALYSIS);
    localStorage.removeItem(LS_PROCESSING_METHOD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_SESSION_PATH);
    localStorage.removeItem(LS_MESSAGE_ID);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);

    const [ohash, nhash] = await Promise.all([
      computeFileHash(oldPdf.bytes),
      computeFileHash(newPdf.bytes),
    ]);
    setOldFileHash(ohash);
    setNewFileHash(nhash);
    lsSet(LS_OLD_HASH, ohash);
    lsSet(LS_NEW_HASH, nhash);

    let currentSession = '';

    try {
      const form = new FormData();
      form.append('old_file', new Blob([oldPdf.bytes], { type: oldPdf.mimeType }), oldPdf.name);
      form.append('new_file', new Blob([newPdf.bytes], { type: newPdf.mimeType }), newPdf.name);
      form.append('old_file_hash', ohash);
      form.append('new_file_hash', nhash);
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      form.append('processor_version', 'structured');

      const controller = new AbortController();
      abortControllerRef.current = controller;

      const startTime = Date.now();
      let firstTokenTime: number | null = null;
      const res = await fetch('/api/compare/analyze', { method: 'POST', body: form, signal: controller.signal });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';
      let fileType = '';
      let processingMethod = 'structured';
      let isCached = false;
      let cachedMessageId: number | null = null;
      let usageData: { input_tokens?: number; output_tokens?: number; thinking_tokens?: number; total_tokens?: number; cost_eur?: number } = {};

      let cachedSessionPath = '';
      let localImageContext: ImagePair[] = [];
      let llmRequestId: number | null = null;
      let streamDone = false;
      let lastPersist = 0;
      let hadWarning = false;
      while (!streamDone) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        let sseError: string | null = null;
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') { streamDone = true; break; }
          try {
            const event = JSON.parse(data);
            if (event.type === 'response.output_text.delta') {
              if (firstTokenTime === null) firstTokenTime = Date.now();
              accumulated += event.delta;
              setAnalysis(accumulated);
              if (Date.now() - lastPersist > LS_STREAM_PERSIST_MS) {
                lsSet(LS_ANALYSIS, accumulated);
                lastPersist = Date.now();
              }
              setParsedJsonItems(parsePartialJsonItems(accumulated));
            } else if (event.type === 'metadata') {
              fileType = event.file_type || '';
              setCurrentFileType(fileType);
              processingMethod = event.method || 'structured';
              lsSet(LS_PROCESSING_METHOD, processingMethod);
              llmRequestId = event.llm_request_id ?? null;
              if (event.cached) {
                isCached = true;
                cachedMessageId = event.message_id ?? null;
                cachedSessionPath = event.session_path ?? '';
              }
            } else if (event.type === 'image_context') {
              localImageContext = event.images || [];
              setImageContext(localImageContext);
            } else if (event.type === 'warning') {
              hadWarning = true;
              toast.warning(event.detail || 'The report may be incomplete.');
            } else if (event.type === 'usage') {
              usageData = event;
            } else if (event.type === 'error') {
              sseError = event.error ?? 'Unknown server error';
              break;
            }
          } catch { /* skip malformed chunks */ }
        }
        if (sseError) throw new Error(sseError);
      }

      lsSet(LS_ANALYSIS, accumulated);
      setParsedJsonItems(parsePartialJsonItems(accumulated));
      // The stream closed without its end marker: the gateway cut it. What we
      // have is a partial report — keep it on screen, but never save it as the
      // result for this file pair (it would then be served from cache to everyone).
      if (!streamDone) throw new Error(STREAM_INTERRUPTED);

      if (isCached && cachedMessageId !== null) {
        // Result from DB cache — reuse the original session folder
        setMessageId(cachedMessageId);
        lsSet(LS_MESSAGE_ID, String(cachedMessageId));
        if (cachedSessionPath) {
          setSessionPath(cachedSessionPath);
          lsSet(LS_SESSION_PATH, cachedSessionPath);
        }
        toast.info('Result retrieved from cache');
      } else {
        const endTime = Date.now();
        const ttftS = firstTokenTime !== null ? (firstTokenTime - startTime) / 1000 : 0;
        const generationS = firstTokenTime !== null ? (endTime - firstTokenTime) / 1000 : (endTime - startTime) / 1000;
        // Create session folder only for fresh (non-cached) analyses
        if (volumeConfigured) {
          currentSession = await autoSavePdfs(oldPdf, newPdf);
          if (currentSession) setSessionPath(currentSession);
        }
        if (currentSession) {
          // Always save the raw analysis text for history restore
          await autoSaveResult(currentSession, 'analysis.md', accumulated);
          autoSaveExcel(currentSession, accumulated, fileType, localImageContext);
        }
        // A report that came with a warning (output cut at the token limit,
        // unreadable document, truncated diff) is saved without hashes: the
        // cache replays the text only, so the warning would be lost next time.
        const mid = await saveToHistory(
          oldPdf.name, newPdf.name, accumulated, '', currentSession, hadWarning ? '' : ohash, hadWarning ? '' : nhash,
          fileType, processingMethod, 'structured', ttftS, generationS, usageData, llmRequestId,
        );
        if (mid !== null) {
          setMessageId(mid);
          lsSet(LS_MESSAGE_ID, String(mid));
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        toast.info('Analysis cancelled — partial result kept.');
        return;
      }
      const raw = err instanceof Error ? err.message : String(err);
      const isNetworkDrop = raw.toLowerCase().includes('failed to fetch')
        || raw.toLowerCase().includes('networkerror')
        || raw.toLowerCase().includes('load failed');
      const msg = isNetworkDrop
        ? 'Connection lost during analysis. Please retry — the server may still be processing.'
        : raw;
      setError(msg);
      toast.error(`Change Table failed: ${msg}`);
    } finally {
      setIsAnalyzing(false);
    }
  };

  const handleAnalyzeStandard = async (forceRefresh = false) => {
    if (!oldPdf || !newPdf) return;
    setError('');
    setAnalysisStd('');
    setImpact(null);
    setImpactError('');
    setSessionPath('');
    setMessageIdStd(null);
    setIsAnalyzingStd(true);
    localStorage.removeItem(LS_ANALYSIS_STD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_SESSION_PATH);
    localStorage.removeItem(LS_MESSAGE_ID_STD);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);

    const [ohash, nhash] = await Promise.all([
      computeFileHash(oldPdf.bytes),
      computeFileHash(newPdf.bytes),
    ]);
    setOldFileHash(ohash);
    setNewFileHash(nhash);
    lsSet(LS_OLD_HASH, ohash);
    lsSet(LS_NEW_HASH, nhash);

    let currentSession = '';

    try {
      const form = new FormData();
      form.append('old_file', new Blob([oldPdf.bytes], { type: oldPdf.mimeType }), oldPdf.name);
      form.append('new_file', new Blob([newPdf.bytes], { type: newPdf.mimeType }), newPdf.name);
      form.append('old_file_hash', ohash);
      form.append('new_file_hash', nhash);
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      form.append('processor_version', 'standard');

      const controller = new AbortController();
      abortControllerRefStd.current = controller;

      const startTime = Date.now();
      let firstTokenTime: number | null = null;
      const res = await fetch('/api/compare/analyze', { method: 'POST', body: form, signal: controller.signal });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';
      let fileType = '';
      let isCached = false;
      let cachedMessageId: number | null = null;
      let usageData: { input_tokens?: number; output_tokens?: number; thinking_tokens?: number; total_tokens?: number; cost_eur?: number } = {};
      let cachedSessionPath = '';
      let llmRequestId: number | null = null;
      let streamDone = false;
      let lastPersist = 0;
      let hadWarning = false;
      while (!streamDone) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        let sseError: string | null = null;
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') { streamDone = true; break; }
          try {
            const event = JSON.parse(data);
            if (event.type === 'response.output_text.delta') {
              if (firstTokenTime === null) firstTokenTime = Date.now();
              accumulated += event.delta;
              setAnalysisStd(accumulated);
              if (Date.now() - lastPersist > LS_STREAM_PERSIST_MS) {
                lsSet(LS_ANALYSIS_STD, accumulated);
                lastPersist = Date.now();
              }
            } else if (event.type === 'metadata') {
              fileType = event.file_type || '';
              setCurrentFileType(fileType);
              llmRequestId = event.llm_request_id ?? null;
              if (event.cached) {
                isCached = true;
                cachedMessageId = event.message_id ?? null;
                cachedSessionPath = event.session_path ?? '';
              }
            } else if (event.type === 'warning') {
              hadWarning = true;
              toast.warning(event.detail || 'The report may be incomplete.');
            } else if (event.type === 'usage') {
              usageData = event;
            } else if (event.type === 'error') {
              sseError = event.error ?? 'Unknown server error';
              break;
            }
          } catch { /* skip malformed chunks */ }
        }
        if (sseError) throw new Error(sseError);
      }

      lsSet(LS_ANALYSIS_STD, accumulated);
      if (!streamDone) throw new Error(STREAM_INTERRUPTED);

      if (isCached && cachedMessageId !== null) {
        setMessageIdStd(cachedMessageId);
        lsSet(LS_MESSAGE_ID_STD, String(cachedMessageId));
        if (cachedSessionPath) {
          setSessionPath(cachedSessionPath);
          lsSet(LS_SESSION_PATH, cachedSessionPath);
        }
        toast.info('Result retrieved from cache');
      } else {
        const endTime = Date.now();
        const ttftS = firstTokenTime !== null ? (firstTokenTime - startTime) / 1000 : 0;
        const generationS = firstTokenTime !== null ? (endTime - firstTokenTime) / 1000 : (endTime - startTime) / 1000;
        if (volumeConfigured) {
          currentSession = await autoSavePdfs(oldPdf, newPdf);
          if (currentSession) setSessionPath(currentSession);
        }
        if (currentSession) {
          await autoSaveResult(currentSession, 'analysis.md', accumulated);
          const docTitle = `${oldPdf.name} vs ${newPdf.name}`;
          autoSavePdf(currentSession, accumulated, docTitle);
        }
        const mid = await saveToHistory(
          oldPdf.name, newPdf.name, accumulated, '', currentSession, hadWarning ? '' : ohash, hadWarning ? '' : nhash,
          fileType, 'standard', 'standard', ttftS, generationS, usageData, llmRequestId,
        );
        if (mid !== null) {
          setMessageIdStd(mid);
          lsSet(LS_MESSAGE_ID_STD, String(mid));
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        toast.info('Analysis cancelled — partial result kept.');
        return;
      }
      const raw = err instanceof Error ? err.message : String(err);
      const isNetworkDrop = raw.toLowerCase().includes('failed to fetch')
        || raw.toLowerCase().includes('networkerror')
        || raw.toLowerCase().includes('load failed');
      const msg = isNetworkDrop
        ? 'Connection lost during analysis. Please retry — the server may still be processing.'
        : raw;
      setError(msg);
      toast.error(`Change Summary failed: ${msg}`);
    } finally {
      setIsAnalyzingStd(false);
    }
  };

  const handleAnalyzeBoth = (forceRefresh = false) => {
    void Promise.allSettled([handleAnalyzeStructured(forceRefresh), handleAnalyzeStandard(forceRefresh)]);
  };

  // -------------------------------------------------------------------------
  // Impact — uses whichever main analysis ran (Change Table preferred,
  // falls back to Change Summary since either produces a usable changes summary),
  // or a hand-typed change description in 'manual' mode.
  // -------------------------------------------------------------------------

  const changesTextForImpact = effectiveImpactMode === 'manual' ? manualChangesText.trim() : (analysis || analysisStd);

  const handleImpact = async (forceRefresh = false) => {
    if (!changesTextForImpact) return;
    setIsImpacting(true);
    setImpact(null);
    setImpactError('');
    localStorage.removeItem(LS_IMPACT);
    try {
      const manual = effectiveImpactMode === 'manual';
      const controller = new AbortController();
      abortControllerRefImpact.current = controller;
      const res = await fetch('/api/compare/impact', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          changes_text: changesTextForImpact,
          // Link the impact_requests audit row back to this comparison's messages row
          // — also the cache key the server looks up unless force_refresh is set.
          // No comparison ran in manual mode, so no hash to key the cache on.
          old_file_hash: manual ? '' : (oldFileHash || ''),
          new_file_hash: manual ? '' : (newFileHash || ''),
          // The compared document itself would be the top hit — the server drops it.
          old_file_name: manual ? '' : (oldPdf?.name || ''),
          new_file_name: manual ? '' : (newPdf?.name || ''),
          force_refresh: forceRefresh,
        }),
      });
      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `Server error: ${res.status}`);
      }
      // NDJSON stream: plan → one document per judged candidate → done | error.
      const reader = res.body.getReader();
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
        setImpact(current);
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
      const final = current as ImpactResult | null;
      if (!final?.done) throw new Error('The search was interrupted before it finished.');
      if (final.cached) toast.info('Result retrieved from cache');
      else if (final.no_changes) toast.info('Analysis reported no substantive changes — nothing to judge.');
      lsSet(LS_IMPACT, JSON.stringify(final));
      // Kept with the comparison it was run on, so reopening the history entry shows it again.
      const historyId = manual ? null : (analysis ? messageId : messageIdStd);
      if (historyId !== null && !final.no_changes) {
        fetch(`/api/history/${historyId}/impact`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ impact_json: JSON.stringify(final) }),
        }).catch(() => { /* best-effort — the result stays on screen and in the server cache */ });
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        toast.info('Impact search cancelled.');
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setImpactError(msg);
      toast.error(`Impact search failed: ${msg}`);
    } finally {
      setIsImpacting(false);
    }
  };

  // -------------------------------------------------------------------------
  // Single-document summary — independent of the diff; cached server-side on
  // the file's own hash, so re-summarizing the same file (even across a
  // different comparison) is instant unless "Re-run" is used.
  // -------------------------------------------------------------------------

  const handleSummarize = async (which: 'old' | 'new', forceRefresh = false) => {
    const doc = which === 'old' ? oldPdf : newPdf;
    if (!doc) return;
    setActiveSummaryTab(which);
    setSummaries(prev => ({ ...prev, [which]: { text: '', error: '', loading: true, noContent: false, meta: null } }));
    try {
      const hash = await computeFileHash(doc.bytes);
      const form = new FormData();
      form.append('file', new Blob([doc.bytes], { type: doc.mimeType }), doc.name);
      form.append('file_hash', hash);
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      const res = await fetch('/api/compare/summarize', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `Server error: ${res.status}`);
      setSummaries(prev => ({
        ...prev,
        [which]: {
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
        },
      }));
      if (data.cached) toast.info('Result retrieved from cache');
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setSummaries(prev => ({ ...prev, [which]: { ...prev[which], loading: false, error: msg } }));
      toast.error(`Summary failed: ${msg}`);
    }
  };

  const downloadSummaryPdf = async (which: 'old' | 'new') => {
    const doc = which === 'old' ? oldPdf : newPdf;
    const text = summaries[which].text;
    if (!doc || !text) return;
    const base = doc.name.replace(/\.[^.]+$/, '') || 'document';
    const form = new FormData();
    form.append('markdown_text', text);
    form.append('title', `Summary — ${doc.name}`);
    form.append('filename', `summary_${base}`);
    try {
      const res = await fetch('/api/compare/export-pdf', { method: 'POST', body: form });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        toast.error(`PDF export failed: ${err.error ?? res.statusText}`);
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `summary_${base}.pdf`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(`PDF export failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const canAnalyze = !!oldPdf && !!newPdf && !!oldPdf.data && !!newPdf.data && !isAnalyzing && !isAnalyzingStd && !isImpacting;
  const hasStandardVersion = availableVersions.some(v => v.id === 'standard');
  const hasStructuredVersion = availableVersions.some(v => v.id === 'structured');

  const accentBlue   = 'var(--color-accent-primary)';
  const accentAmber  = '#d97706';
  const accentGreen  = '#16a34a';

  // --- Render ---

  return (
    <div className="max-w-7xl mx-auto px-4 py-10 space-y-8">

      {/* ── Header ── */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight" style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}>
            Document Comparison
          </h1>
          <p className="text-sm" style={{ color: 'var(--color-text-muted)' }}>
            Upload two documents (PDF, DOCX, PPTX, Excel, image…) to detect differences.
          </p>
        </div>
        <div className="flex-shrink-0 flex items-center gap-2">
          <WhatsNewButton tabId="compare" open={showWhatsNew} onClick={() => setShowWhatsNew(o => !o)} />
          {(oldPdf || newPdf || analysis || analysisStd || manualChangesText) && (
            <button
              onClick={clearAll}
              disabled={isAnalyzing || isAnalyzingStd || isImpacting}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer disabled:opacity-40"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-error)'; e.currentTarget.style.color = 'var(--color-error)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
            >
              <Trash2 className="h-4 w-4" />
              Clear all
            </button>
          )}
          <button
            onClick={() => setHistoryOpen(true)}
            className="flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer"
            style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
          >
            <History className="h-4 w-4" />
            History
          </button>
        </div>
      </div>

      {showWhatsNew && <WhatsNewModal tabId="compare" onClose={() => setShowWhatsNew(false)} />}

      {/* ── Disclaimer banner ── */}
      <div className="flex items-start gap-3 px-4 py-3.5 rounded-xl border" style={{ borderColor: '#d97706', background: '#fffbeb' }}>
        <Info className="h-4 w-4 mt-0.5 flex-shrink-0" style={{ color: '#d97706' }} />
        <p className="text-sm leading-relaxed" style={{ color: '#92400e' }}>
          This tool is intended to compare two versions of the same document.
        </p>
      </div>

      <ComparisonHistory
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        onLoad={handleLoadFromHistory}
      />

      {/* ── Error banner ── */}
      {error && (
        <div className="flex items-start gap-3 px-4 py-3.5 rounded-xl border border-[var(--color-error)]/25 bg-[var(--color-error)]/5">
          <AlertCircle className="h-4 w-4 text-[var(--color-error)] mt-0.5 flex-shrink-0" />
          <p className="text-sm text-[var(--color-error)] leading-relaxed">{error}</p>
        </div>
      )}

      {/* ── Upload + Actions card ── */}
      <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-bg-secondary)]/30 p-6 space-y-5">

        {/* Drop zones */}
        <div className="flex gap-4">
          <PdfDropZone
            label="Document A (Old)"
            step={1}
            file={oldPdf}
            onFile={(f) => { clearAnalysisState(); setSummaries(prev => ({ ...prev, old: EMPTY_SUMMARY })); setOldPdfAndPersist(f); }}
            onRemove={() => { clearAnalysisState(); setSummaries(prev => ({ ...prev, old: EMPTY_SUMMARY })); setOldPdfAndPersist(null); }}
            onInvalidFile={handleInvalidFile}
            disabled={isAnalyzing || isAnalyzingStd || isImpacting}
          />
          <PdfDropZone
            label="Document B (New)"
            step={2}
            file={newPdf}
            onFile={(f) => { clearAnalysisState(); setSummaries(prev => ({ ...prev, new: EMPTY_SUMMARY })); setNewPdfAndPersist(f); }}
            onRemove={() => { clearAnalysisState(); setSummaries(prev => ({ ...prev, new: EMPTY_SUMMARY })); setNewPdfAndPersist(null); }}
            onInvalidFile={handleInvalidFile}
            disabled={isAnalyzing || isAnalyzingStd || isImpacting}
          />
        </div>

        {/* Quick per-document summary — independent of the diff, works as soon as either file is uploaded */}
        {(oldPdf || newPdf) && (
          <div className="flex flex-wrap items-center gap-3">
            {oldPdf && (
              <>
                <button
                  onClick={() => handleSummarize('old')}
                  disabled={summaries.old.loading}
                  className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
                  style={{ borderColor: accentGreen, color: accentGreen, background: 'transparent' }}
                  title="Summarize Document A on its own — no comparison needed"
                >
                  {summaries.old.loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                  {summaries.old.loading ? 'Summarizing…' : 'Summarize Document A'}
                </button>
                {summaries.old.text && !summaries.old.loading && (
                  <button
                    onClick={() => handleSummarize('old', true)}
                    className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
                    style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
                    title="Force a fresh summary of Document A, ignoring cached results"
                  >
                    <RotateCcw className="h-4 w-4" />
                    Re-run
                  </button>
                )}
              </>
            )}
            {newPdf && (
              <>
                <button
                  onClick={() => handleSummarize('new')}
                  disabled={summaries.new.loading}
                  className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
                  style={{ borderColor: accentGreen, color: accentGreen, background: 'transparent' }}
                  title="Summarize Document B on its own — no comparison needed"
                >
                  {summaries.new.loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                  {summaries.new.loading ? 'Summarizing…' : 'Summarize Document B'}
                </button>
                {summaries.new.text && !summaries.new.loading && (
                  <button
                    onClick={() => handleSummarize('new', true)}
                    className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
                    style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
                    title="Force a fresh summary of Document B, ignoring cached results"
                  >
                    <RotateCcw className="h-4 w-4" />
                    Re-run
                  </button>
                )}
              </>
            )}
          </div>
        )}

        {(() => {
          const oldActive = !!(summaries.old.text || summaries.old.loading || summaries.old.error || summaries.old.noContent);
          const newActive = !!(summaries.new.text || summaries.new.loading || summaries.new.error || summaries.new.noContent);
          if (!oldActive && !newActive) return null;
          const bothActive = oldActive && newActive;
          // Always show exactly one card, full width — a tab switch when both are
          // active, rather than splitting the width between two half-size cards.
          const shown: 'old' | 'new' = bothActive ? activeSummaryTab : (oldActive ? 'old' : 'new');
          return (
            <div className="space-y-3">
              {bothActive && (
                <div className="inline-flex rounded-xl border border-[var(--color-border)]/40 p-1 gap-1">
                  {(['old', 'new'] as const).map(which => (
                    <button
                      key={which}
                      onClick={() => setActiveSummaryTab(which)}
                      className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                      style={shown === which
                        ? { background: accentGreen, color: 'white' }
                        : { color: 'var(--color-text-muted)', background: 'transparent' }}
                    >
                      {which === 'old' ? 'Document A' : 'Document B'}
                    </button>
                  ))}
                </div>
              )}
              <DocSummaryCard
                title={shown === 'old' ? 'Document A Summary' : 'Document B Summary'}
                state={shown === 'old' ? summaries.old : summaries.new}
                accentColor={accentGreen}
                onDownload={() => downloadSummaryPdf(shown)}
              />
            </div>
          );
        })()}

        {/* Divider */}
        <div className="border-t border-[var(--color-border)]/30" />

        {/* Actions row — no version selector: both outputs are independent buttons */}
        <div className="flex flex-wrap items-center gap-3">
          {hasStandardVersion && (
            <button
              onClick={() => handleAnalyzeStandard()}
              disabled={!canAnalyze}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold text-white shadow-sm transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none cursor-pointer"
              style={{
                background: canAnalyze
                  ? 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)'
                  : 'var(--color-border)',
                boxShadow: canAnalyze ? '0 2px 8px rgba(0,85,164,0.35)' : undefined,
              }}
              title="Narrative diff grouped by section — one bullet per change."
            >
              {isAnalyzingStd
                ? <Loader2 className="h-4 w-4 animate-spin" />
                : <ArrowRight className="h-4 w-4" />}
              {isAnalyzingStd ? 'Generating…' : 'Generate Change Summary'}
            </button>
          )}
          {isAnalyzingStd && (
            <button
              onClick={() => abortControllerRefStd.current?.abort()}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-body)' }}
            >
              <Square className="h-3.5 w-3.5" />
              Cancel
            </button>
          )}
          {analysisStd && canAnalyze && (
            <button
              onClick={() => handleAnalyzeStandard(true)}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
              title="Force a fresh Change Summary, ignoring cached results"
            >
              <RotateCcw className="h-4 w-4" />
              Re-run
            </button>
          )}

          {hasStructuredVersion && (
            <button
              onClick={() => handleAnalyzeStructured()}
              disabled={!canAnalyze}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed border cursor-pointer"
              style={{ borderColor: accentBlue, color: canAnalyze ? accentBlue : 'var(--color-text-muted)', background: 'transparent' }}
              title="Structured change table with type and criticality. Exportable to Excel."
            >
              {isAnalyzing
                ? <Loader2 className="h-4 w-4 animate-spin" />
                : <ArrowRight className="h-4 w-4" />}
              {isAnalyzing ? 'Generating…' : 'Generate Change Table'}
            </button>
          )}
          {isAnalyzing && (
            <button
              onClick={() => abortControllerRef.current?.abort()}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-body)' }}
            >
              <Square className="h-3.5 w-3.5" />
              Cancel
            </button>
          )}
          {analysis && canAnalyze && (
            <button
              onClick={() => handleAnalyzeStructured(true)}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
              title="Force a fresh Change Table, ignoring cached results"
            >
              <RotateCcw className="h-4 w-4" />
              Re-run
            </button>
          )}

          {hasStandardVersion && hasStructuredVersion && (
            <button
              onClick={() => handleAnalyzeBoth()}
              disabled={!canAnalyze}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
            >
              <ArrowRight className="h-4 w-4" />
              Run Both
            </button>
          )}
        </div>

      </div>

      {/* ── Results ── */}
      {(analysis || isAnalyzing || analysisStd || isAnalyzingStd) && currentFileType === 'docx' && (
        <p className="text-xs text-[var(--color-text-muted)] flex items-center gap-1.5">
          <span>⚠️</span>
          <span><strong>Page numbers are approximate</strong> (DOCX limitation) — actual change may be on an adjacent page.</span>
        </p>
      )}
      {(() => {
        const stdActive = !!(analysisStd || isAnalyzingStd);
        const structActive = !!(analysis || isAnalyzing);
        if (!stdActive && !structActive) return null;
        const bothActive = stdActive && structActive;
        // Always show exactly one card, full width — a tab switch when both are
        // active, rather than splitting the width between two half-size cards
        // (same reasoning as the Document Summary A/B tab switcher above).
        const shown: 'summary' | 'table' = bothActive ? activeAnalysisTab : (stdActive ? 'summary' : 'table');
        return (
          <div className="space-y-3">
            {bothActive && (
              <div className="inline-flex rounded-xl border border-[var(--color-border)]/40 p-1 gap-1">
                {(['summary', 'table'] as const).map(which => (
                  <button
                    key={which}
                    onClick={() => setActiveAnalysisTab(which)}
                    className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                    style={shown === which
                      ? { background: accentBlue, color: 'white' }
                      : { color: 'var(--color-text-muted)', background: 'transparent' }}
                  >
                    {which === 'summary' ? 'Change Summary' : 'Change Table'}
                  </button>
                ))}
              </div>
            )}
            {shown === 'summary' ? (
              <ResultCard
                title="Change Summary"
                icon={FileText}
                accentColor={accentBlue}
                content={analysisStd}
                isStreaming={isAnalyzingStd}
                onDownload={async () => {
                  const base = newPdf?.name.replace(/\.[^.]+$/, '') ?? 'document';
                  const form = new FormData();
                  form.append('markdown_text', analysisStd);
                  form.append('title', `Analysis — ${base}`);
                  form.append('filename', `analysis_${base}`);
                  try {
                    const res = await fetch('/api/compare/export-pdf', { method: 'POST', body: form });
                    if (!res.ok) {
                      const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
                      toast.error(`PDF export failed: ${err.error ?? res.statusText}`);
                      return;
                    }
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url; a.download = `analysis_${base}.pdf`; a.click();
                    URL.revokeObjectURL(url);
                  } catch (e) {
                    toast.error(`PDF export failed: ${e instanceof Error ? e.message : String(e)}`);
                  }
                }}
                feedbackProps={
                  analysisStd && !isAnalyzingStd && oldPdf && newPdf
                    ? {
                        submissionKey: messageIdStd !== null
                          ? `mid-${messageIdStd}`
                          : `hash-${oldFileHash.slice(0, 16)}-${newFileHash.slice(0, 16)}-std`,
                        messageId: messageIdStd,
                      }
                    : undefined
                }
              />
            ) : (
              <StructuredResultCard
                items={parsedJsonItems}
                isStreaming={isAnalyzing}
                accentColor={accentBlue}
                fileType={currentFileType}
                imageContext={imageContext}
                onExportExcel={async () => {
                  try {
                    const base = newPdf?.name.replace(/\.[^.]+$/, '') ?? 'comparison';
                    const form = new FormData();
                    form.append('json_text', analysis);
                    form.append('filename', `analysis_${base}`);
                    form.append('file_type', currentFileType);
                    if (imageContext.length > 0) {
                      form.append('image_pairs_json', JSON.stringify(imageContext));
                    }
                    const res = await fetch('/api/compare/export-excel', { method: 'POST', body: form });
                    if (!res.ok) {
                      const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
                      toast.error(`Excel export failed: ${err.error ?? res.statusText}`);
                      return;
                    }
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `analysis_${base}.xlsx`;
                    a.click();
                    URL.revokeObjectURL(url);
                  } catch (e) {
                    toast.error(`Excel export failed: ${e instanceof Error ? e.message : String(e)}`);
                  }
                }}
                feedbackProps={
                  analysis && !isAnalyzing && oldPdf && newPdf
                    ? {
                        submissionKey: messageId !== null
                          ? `mid-${messageId}`
                          : `hash-${oldFileHash.slice(0, 16)}-${newFileHash.slice(0, 16)}`,
                        messageId,
                      }
                    : undefined
                }
              />
            )}
          </div>
        );
      })()}

      {impactConfigured && (
        <div className="space-y-3">
          <div className="rounded-xl border px-4 py-2.5 text-xs" style={{ borderColor: '#d97706', background: '#fffbeb', color: '#92400e' }}>
            This section is in testing phase — results may be incomplete or inaccurate.
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-semibold" style={{ color: 'var(--color-text-heading)' }}>Impact Search</h2>
            {IMPACT_MANUAL_MODE_ENABLED && (
              <div className="inline-flex rounded-xl border border-[var(--color-border)]/40 p-1 gap-1">
                <button
                  onClick={() => switchImpactMode('compare')}
                  className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                  style={effectiveImpactMode === 'compare'
                    ? { background: accentAmber, color: 'white' }
                    : { color: 'var(--color-text-muted)', background: 'transparent' }}
                >
                  Use Compare result
                </button>
                <button
                  onClick={() => switchImpactMode('manual')}
                  className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                  style={effectiveImpactMode === 'manual'
                    ? { background: accentAmber, color: 'white' }
                    : { color: 'var(--color-text-muted)', background: 'transparent' }}
                  title="No old/new document to upload — just describe the change (e.g. from an email) to search for impacted documents"
                >
                  Describe a change manually
                </button>
              </div>
            )}
          </div>

          {effectiveImpactMode === 'manual' ? (
            <textarea
              value={manualChangesText}
              onChange={(e) => setManualChangesTextAndPersist(e.target.value)}
              disabled={isImpacting}
              placeholder="Paste or type a description of the change — e.g. an email announcing a new torque value, a general idea of what's changing…"
              rows={5}
              className="w-full rounded-xl border p-4 text-sm resize-y disabled:opacity-60"
              style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-primary)', color: 'var(--color-text-body)' }}
            />
          ) : (
            changesTextForImpact && !isAnalyzing && !isAnalyzingStd && (
              <p className="text-xs -mb-1" style={{ color: 'var(--color-text-muted)' }}>
                Using: <strong>{analysis ? 'Change Table' : 'Change Summary'}</strong> as the change summary
                {analysis && analysisStd && ' (Change Table takes priority when both are available)'}
              </p>
            )
          )}
        </div>
      )}
      {changesTextForImpact && !isAnalyzing && !isAnalyzingStd && impactConfigured && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={() => handleImpact()}
            disabled={isImpacting}
            className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
            style={{ borderColor: accentAmber, color: accentAmber, background: 'transparent' }}
          >
            {isImpacting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
            {isImpacting ? 'Judging…' : 'Judge Impacted Docs'}
          </button>
          {isImpacting && (
            <button
              onClick={() => abortControllerRefImpact.current?.abort()}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-body)' }}
            >
              <Square className="h-3.5 w-3.5" />
              Cancel
            </button>
          )}
          {impact?.done && !isImpacting && (
            <button
              onClick={() => handleImpact(true)}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
              title="Force a fresh search, ignoring cached results"
            >
              <RotateCcw className="h-4 w-4" />
              Re-run
            </button>
          )}
        </div>
      )}

      {(impact || isImpacting || impactError) && (
        <ImpactResultsCard
          result={impact}
          isLoading={isImpacting}
          error={impactError}
          accentColor={accentAmber}
          changeTableShown={parsedJsonItems.length > 0 && !isAnalyzing}
          messageId={analysis ? messageId : messageIdStd}
          oldFileHash={oldFileHash}
          newFileHash={newFileHash}
          exportName={`impact_${(newPdf?.name || oldPdf?.name || 'search').replace(/\.[^.]+$/, '')}`}
        />
      )}
    </div>
  );
}
