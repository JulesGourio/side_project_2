import React from "react";
import { Copy, Check } from "lucide-react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Prism as SyntaxHighlighter,
  SyntaxHighlighterProps,
} from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

// Type workaround for react-syntax-highlighter with React 18
const SyntaxHighlighterComponent =
  SyntaxHighlighter as unknown as React.ComponentType<SyntaxHighlighterProps>;

/**
 * Syntax-highlighted code block with a copy button.
 */
function CodeBlock({ language, value }: { language: string; value: string }) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative group my-4">
      <div className="absolute right-2 top-2 z-10">
        <button
          onClick={handleCopy}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-[var(--color-background)]/90 backdrop-blur-sm border border-[var(--color-border)] text-xs font-medium text-[var(--color-text-primary)] opacity-0 group-hover:opacity-100 transition-opacity hover:bg-[var(--color-muted)] hover:scale-105 active:scale-95"
          title="Copy code"
        >
          {copied ? (
            <>
              <Check className="h-3 w-3" />
              <span>Copied!</span>
            </>
          ) : (
            <>
              <Copy className="h-3 w-3" />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <SyntaxHighlighterComponent
        language={language || "text"}
        style={oneDark}
        customStyle={{
          margin: 0,
          borderRadius: "0.75rem",
          fontSize: "0.875rem",
          lineHeight: "1.6",
          padding: "1rem",
          background: "rgba(15, 23, 42, 0.8)",
        }}
        showLineNumbers={value.split("\n").length > 3}
        wrapLines={true}
        wrapLongLines={false}
      >
        {value}
      </SyntaxHighlighterComponent>
      {language && (
        <div className="absolute left-3 top-2 text-xs font-medium text-[var(--color-text-muted)] opacity-60">
          {language}
        </div>
      )}
    </div>
  );
}

/**
 * Strip footnotes and source references that Knowledge Assistants inject.
 * Handles both HTML footnotes and GFM markdown footnotes.
 */
function stripFootnotes(text: string): string {
  let cleaned = text;

  // Remove HTML footnote sections: <section class="footnotes">...</section>
  cleaned = cleaned.replace(
    /<section[^>]*class="footnotes?"[^>]*>[\s\S]*?<\/section>/gi,
    "",
  );
  // Remove standalone HTML footnote lists: <ol> blocks that contain footnote <li>s
  cleaned = cleaned.replace(
    /<ol[^>]*class="[^"]*footnote[^"]*"[^>]*>[\s\S]*?<\/ol>/gi,
    "",
  );
  // Remove inline HTML footnote refs: <sup>[1]</sup>, <sup>1</sup>, <sup><a ...>1</a></sup>
  cleaned = cleaned.replace(/<sup[^>]*>[\s\S]*?<\/sup>/gi, "");
  // Remove other stray HTML tags (KA sometimes emits bare tags that react-markdown can't parse)
  cleaned = cleaned.replace(/<\/?(?:section|sup|a\s+href="#fn)[^>]*>/gi, "");

  // Remove GFM footnote definitions: [^1]: some text (at start of line)
  cleaned = cleaned.replace(/^\[\^\d+\]:.*$/gm, "");
  // Remove GFM inline footnote references: [^1]
  cleaned = cleaned.replace(/\[\^\d+\]/g, "");

  // Clean up excess blank lines left behind
  cleaned = cleaned.replace(/\n{3,}/g, "\n\n");

  return cleaned.trim();
}

/** Maps a 1-based citation number to the source it points to. */
export type CitationMap = Record<number, { url?: string; title?: string }>;

/**
 * Turn every mention of a cited document's reference into a clickable
 * superscript [n] link, inline in the answer.
 *
 * The Knowledge Assistant returns the answer text + a flat list of consulted
 * documents, but NOT the character position of each citation. So instead of
 * guessing positions, we anchor each citation on the document's own reference
 * code wherever it appears in the text (the "📋 Référence : …" header and any
 * inline mentions). Each occurrence of the ref `T` for source `n` becomes
 * `T[n](cite:n)`, which the `a` renderer styles as a superscript badge.
 *
 * If the content already carries server-injected ⟦n⟧ markers (an endpoint that
 * does expose positions), those win and we don't touch the text.
 */
function linkifyCitations(text: string, citations?: CitationMap): string {
  if (!citations || /⟦\d+⟧/.test(text)) return text;
  let out = text;
  for (const [nStr, c] of Object.entries(citations)) {
    const title = (c.title || "").trim();
    if (!title) continue;
    const esc = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    // Match the ref when it is not glued to other word characters, and not
    // already immediately followed by a citation marker.
    const re = new RegExp(`(?<![\\w-])${esc}(?![\\w-])(?!\\s*\\[\\d+\\]\\(cite:)`, "g");
    out = out.replace(re, (m) => `${m}[${nStr}](cite:${nStr})`);
  }
  return out;
}

interface MarkdownRendererProps {
  content: string;
  className?: string;
  // When provided, inline ⟦n⟧ markers in the content are rendered as
  // superscript [n] links to citations[n] (the Databricks-Agent style).
  citations?: CitationMap;
}

/**
 * Shared markdown renderer with themed styling.
 * Used by both the chat Message component and the Compare view.
 */
export function MarkdownRenderer({
  content,
  className = "",
  citations,
}: MarkdownRendererProps) {
  // Strip the endpoint's own footnotes, anchor citations on the referenced
  // document codes in the text, then turn both server-injected ⟦n⟧ markers and
  // the linkified refs into `cite:n` links the `a` renderer styles as badges.
  const cleanedContent = React.useMemo(
    () =>
      linkifyCitations(stripFootnotes(content), citations)
        .replace(/⟦(\d+)⟧/g, (_m, n) => `[${n}](cite:${n})`),
    [content, citations],
  );

  return (
    <div
      className={`prose prose-sm max-w-none break-words text-[var(--color-text-primary)] ${className}`}
      style={{ fontFamily: "var(--font-body)" }}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // Keep our internal `cite:n` links — react-markdown's default sanitizer
        // strips unknown protocols (so the citation badge would never render).
        urlTransform={(url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url))}
        components={{
          // Headings
          h1: ({ children }) => (
            <h1
              className="text-xl font-bold text-[var(--color-text-heading)] mt-5 mb-2.5 pb-1.5 border-b border-[var(--color-border)] tracking-[-0.025em] leading-tight"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2
              className="text-lg font-bold text-[var(--color-text-heading)] mt-4 mb-2 pb-1 border-b border-[var(--color-border)]/60 tracking-[-0.02em] leading-snug"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3
              className="text-base font-semibold text-[var(--color-text-heading)] mt-4 mb-1.5 tracking-[-0.015em] leading-snug"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </h3>
          ),
          h4: ({ children }) => (
            <h4
              className="text-[0.9375rem] font-semibold text-[var(--color-text-heading)] mt-3 mb-1 tracking-[-0.01em]"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </h4>
          ),
          h5: ({ children }) => (
            <h5
              className="text-sm font-semibold text-[var(--color-text-heading)] mt-3 mb-1 uppercase tracking-wide"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </h5>
          ),
          h6: ({ children }) => (
            <h6
              className="text-xs font-semibold text-[var(--color-text-muted)] mt-2 mb-1 uppercase tracking-wider"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </h6>
          ),

          // Paragraphs
          p: ({ children }) => (
            <p className="mb-2.5 leading-[1.7] text-[0.9375rem] text-[var(--color-text-primary)] tracking-[-0.005em]">
              {children}
            </p>
          ),

          // Lists
          ul: ({ children }) => (
            <ul className="my-2.5 ml-5 space-y-1.5 list-disc marker:text-[var(--color-accent-primary)]">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="my-2.5 ml-5 space-y-1.5 list-decimal marker:text-[var(--color-accent-primary)] marker:font-semibold">
              {children}
            </ol>
          ),
          li: ({ children }) => (
            <li className="leading-[1.65] text-[0.9rem] text-[var(--color-text-primary)] pl-1">
              {children}
            </li>
          ),

          // Links
          a: ({ href, children }) => {
            // Inline citation marker: render as a small superscript badge that
            // links to the source (or just shows the reference if no URL).
            if (href && href.startsWith("cite:")) {
              const n = parseInt(href.slice(5), 10);
              const cite = citations?.[n];
              const tip = cite?.title
                ? cite.url
                  ? `${cite.title}\n${cite.url}`
                  : cite.title
                : `Source ${n}`;
              const badge = (
                <sup
                  className="inline-flex items-center justify-center align-super mx-[1px] h-[0.95rem] min-w-[0.95rem] px-[3px] rounded-full text-[0.6rem] font-bold leading-none no-underline"
                  style={{ background: "var(--color-accent-primary)", color: "#fff" }}
                >
                  {Number.isNaN(n) ? "?" : n}
                </sup>
              );
              return cite?.url ? (
                <a href={cite.url} target="_blank" rel="noopener noreferrer" title={tip} className="no-underline">
                  {badge}
                </a>
              ) : (
                <span title={tip} className="cursor-default">{badge}</span>
              );
            }
            return (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[var(--color-accent-primary)] hover:text-[var(--color-accent-primary)]/80 underline decoration-[var(--color-accent-primary)]/30 hover:decoration-[var(--color-accent-primary)] underline-offset-2 transition-colors font-medium"
              >
                {children}
              </a>
            );
          },

          // Code
          code: ({ className, children, ...props }: any) => {
            const match = /language-(\w+)/.exec(className || "");
            const language = match ? match[1] : "";
            const value = String(children).replace(/\n$/, "");

            if (match || value.includes("\n")) {
              return <CodeBlock language={language} value={value} />;
            }

            return (
              <code
                className="bg-[var(--color-muted)]/50 text-[var(--color-accent-primary)] px-1.5 py-0.5 rounded text-[0.8125rem] border border-[var(--color-border)] font-medium"
                style={{ fontFamily: "var(--font-mono)" }}
                {...props}
              >
                {children}
              </code>
            );
          },

          pre: ({ children }) => <>{children}</>,

          // Blockquotes
          blockquote: ({ children }) => (
            <blockquote className="my-4 pl-4 pr-3 py-2.5 border-l-4 border-[var(--color-accent-primary)] bg-[var(--color-muted)]/30 rounded-r-lg">
              <div
                className="text-[var(--color-text-primary)] italic text-[0.9375rem] leading-[1.65]"
                style={{ fontFamily: "var(--font-body)" }}
              >
                {children}
              </div>
            </blockquote>
          ),

          // Tables
          table: ({ children }) => (
            <div className="my-4 overflow-x-auto rounded-xl border border-[var(--color-border)] shadow-md bg-[var(--color-background)]/50 backdrop-blur-sm">
              <table className="w-full border-collapse">{children}</table>
            </div>
          ),
          thead: ({ children }) => (
            <thead className="bg-[var(--color-accent-primary)]/8 sticky top-0 z-10">
              {children}
            </thead>
          ),
          tbody: ({ children }) => (
            <tbody className="divide-y divide-[var(--color-border)]/50">
              {children}
            </tbody>
          ),
          tr: ({ children }) => (
            <tr className="hover:bg-[var(--color-muted)]/40 transition-all duration-200 border-b border-[var(--color-border)]/30">
              {children}
            </tr>
          ),
          th: ({ children }) => (
            <th
              className="px-4 py-3 text-left text-[0.6875rem] font-bold text-[var(--color-text-heading)] uppercase tracking-widest border-b-2 border-[var(--color-accent-primary)]/40"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td
              className="px-4 py-3 text-[0.8125rem] text-[var(--color-text-primary)] tabular-nums"
              style={{ fontFamily: "var(--font-body)" }}
            >
              {children}
            </td>
          ),

          // Horizontal rule
          hr: () => (
            <hr className="my-6 border-t-2 border-[var(--color-border)] opacity-50" />
          ),

          // Strong / Emphasis
          strong: ({ children }) => (
            <strong
              className="font-bold text-[var(--color-text-heading)]"
              style={{ fontFamily: "var(--font-heading)" }}
            >
              {children}
            </strong>
          ),
          em: ({ children }) => (
            <em className="italic text-[var(--color-text-primary)] opacity-90">
              {children}
            </em>
          ),
        }}
      >
        {cleanedContent}
      </ReactMarkdown>
    </div>
  );
}
