import React from "react";
import {
  Prism as SyntaxHighlighter,
  SyntaxHighlighterProps,
} from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

// Type workaround for the syntax highlighter with React 18
const SyntaxHighlighterComponent =
  SyntaxHighlighter as unknown as React.ComponentType<SyntaxHighlighterProps>;

/** A highlighted code block; loaded on demand so the highlighter stays out of the main bundle. */
export default function CodeHighlighter({ language, value }: { language: string; value: string }) {
  return (
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
  );
}
