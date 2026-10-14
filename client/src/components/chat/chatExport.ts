import { downloadPdf, type PdfSection, type PdfSource } from '@/lib/exportPdf';
import { versionLabel, type Message, type Source } from './message';

/** Inline citation markers (⟦n⟧) are for on-screen rendering only: plain [n] for downloads. */
function stripCiteMarkers(content: string): string {
  return content.replace(/⟦(\d+)⟧/g, '[$1]');
}

function sourcesMarkdown(sources: Source[] | undefined): string {
  if (!sources?.length) return '';
  const cited = sources.filter(s => s.n != null).sort((a, b) => (a.n ?? 0) - (b.n ?? 0));
  const others = sources.filter(s => s.n == null);
  const line = (s: Source) => [s.n != null ? `[${s.n}] ${s.title}` : s.title, s.doc_title, versionLabel(s), s.url]
    .filter(Boolean).join(' — ');
  return `\n\n**Sources**\n\n${[...cited, ...others].map(s => `- ${line(s)}`).join('\n')}`;
}

function pdfSources(sources: Source[] | undefined): PdfSource[] {
  return [...(sources ?? [])]
    .sort((a, b) => (a.n ?? Infinity) - (b.n ?? Infinity))
    .map(s => ({
      title: s.title, url: s.url, n: s.n,
      detail: [s.doc_title, versionLabel(s)].filter(Boolean).join(' · '),
    }));
}

function answerSection(msg: Message, question?: string): PdfSection {
  return { markdown: stripCiteMarkers(msg.content), question, sources: pdfSources(msg.sources) };
}

function questionFor(message: Message, messages: Message[]): string | undefined {
  const idx = messages.findIndex(m => m.id === message.id);
  for (let i = idx - 1; i >= 0; i--) {
    if (messages[i].role === 'user') return messages[i].content;
  }
  return undefined;
}

function sessionSections(messages: Message[]): PdfSection[] {
  const sections: PdfSection[] = [];
  let question: string | undefined;
  for (const msg of messages) {
    if (msg.role === 'user') question = msg.content;
    else if (!msg.error && msg.content) {
      sections.push(answerSection(msg, question));
      question = undefined;
    }
  }
  return sections;
}

export function responseMarkdown(message: Message): string {
  return stripCiteMarkers(message.content) + sourcesMarkdown(message.sources);
}

export function sessionMarkdown(messages: Message[]): string {
  const parts = [`# QualiBOT — Session ${new Date().toLocaleDateString('en-GB')}\n`];
  for (const msg of messages) {
    if (msg.role === 'user') parts.push(`## Question\n\n${msg.content}`);
    else if (!msg.error && msg.content) parts.push(`## Response\n\n${responseMarkdown(msg)}`);
  }
  return parts.join('\n\n---\n\n');
}

export function downloadResponsePdf(message: Message, allMessages: Message[], slug: string): Promise<void> {
  const question = questionFor(message, allMessages)?.trim();
  const asTitle = !!question && question.length <= 160;
  return downloadPdf({
    kind: 'chat_response',
    title: asTitle ? question : 'Chatbot answer',
    filename: `qualibot-answer-${slug}`,
    sections: [answerSection(message, asTitle ? undefined : question)],
  });
}

export function downloadSessionPdf(allMessages: Message[], slug: string): Promise<void> {
  const sections = sessionSections(allMessages);
  return downloadPdf({
    kind: 'chat_session',
    title: 'Chatbot conversation',
    filename: `qualibot-conversation-${slug}`,
    meta: [{ label: 'Questions', value: String(sections.length) }],
    sections,
  });
}
