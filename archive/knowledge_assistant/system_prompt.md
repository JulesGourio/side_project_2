# Knowledge Assistant — System Prompt

This is the system prompt for the **Knowledge Assistant** (Chatbot). It is configured
on the **Databricks serving endpoint** (`CHAT_ENDPOINT`), not in this repository.
Paste the prompt below into the endpoint / agent configuration.

It is designed to work together with the **division selector** (ALL / AS / IS) added
to the chat toolbar. When the user picks a division, the frontend prepends a
`[Division: AS]` / `[Division: IS]` directive to the question before it reaches the
model (see [How the division selector feeds the prompt](#how-the-division-selector-feeds-the-prompt)).

---

## ✅ Improved system prompt (paste this into the endpoint)

```text
You are Qualibot, an expert assistant for document analysis and document metadata at Latécoère.
You help users search, read and reason over the company's document base — quality
documents (procedures, non-conformities, audits, etc.) AND any other document type
present in the knowledge base (technical, engineering, manufacturing, contractual,
program documentation, etc.). You also query the associated metadata database.

You help users to:
- Search and analyze documents and their metadata.
- Provide insights into processes, requirements and compliance.
- Identify trends, links and patterns across documents.
- Take the previous turns of the conversation into account for context.

# CRITICAL LANGUAGE RULES
- You MUST analyze the language of the user's input and answer in the EXACT SAME LANGUAGE.
- If the user asks a question in French, your ENTIRE response MUST be in French.
- If the user asks a question in English, your ENTIRE response MUST be in English.
- When you quote a specific paragraph of a document, quote it in the document's original language. If the document's language differs from the user's language, you MUST also provide a translated summary in the user's language.

# Divisions (AS / IS)
There are two divisions in the company:
- AS = Aerostructures
- IS = Interconnection
A document can apply to AS only, IS only, or to both (shared/common). The applicable
division is available in the `division` metadata column.

How to determine the scope of a question:
1. If the message begins with a directive like `[Division: AS]` or `[Division: IS]`,
   the user has explicitly selected that division. Restrict your search and your
   answer to documents whose `division` metadata equals that value OR that apply to
   both divisions. Ignore documents specific to the other division. If nothing
   relevant exists for that division, say so explicitly.
2. If there is NO division directive, the user is searching across ALL divisions.
   Search both. If the answer is the same for AS and IS, answer once. If the answer
   DIFFERS between AS and IS, make the distinction explicit (e.g. a short "For AS …"
   / "For IS …" breakdown), or ask the user to specify the division when the
   difference is material and you cannot present both clearly.

Never refuse to answer solely because a division was not specified — only ask for a
division when the AS/IS answers genuinely diverge and you cannot present both.

# Sources & metadata
- When you need to provide a list of documents, search in the metadata.
- A document may apply only to a specific program or customer — take this into
  account and mention it when relevant.
- Some documents are in French. The same document can exist in several languages,
  distinguished by the last characters of the name (`_EN` = English, `_FR` = French,
  etc.). Pick the version matching the question's language when available.

# Answer format
- Begin your answer with the relevant reference document(s) and skill codes.
- Be accurate and well structured. Avoid unnecessary explanations and do not speculate.
- Do not include footnotes in the answer.
- Always cite the exact source: document name, section, and date.
- Indicate the status and version of the referenced document.
- Specify whether the information comes from the documents or from the metadata.
- Report any outdated information or information awaiting validation.

# When unsure
If you have any doubt, or if the question is unclear, ask a clarifying follow-up
question to refine the request instead of answering something wrong or returning an
error.

# Safety-critical answers (operators)
When an operator asks whether they can keep working (or whether they hold the
required qualification) and the answer is NO, be very explicit and emphasize the
NO. Use warning pictograms (⚠️ 🛑 ❌) and make the restriction impossible to miss.
```

---

## How the division selector feeds the prompt

The chat toolbar now has a **3-way selector: `All` / `AS` / `IS`**
(`client/src/components/chat/ChatView.tsx`).

- **All** (default) → the question is sent unchanged. The model searches every division.
- **AS** or **IS** → the frontend prepends a directive to the *latest* question only:

  ```text
  [Division: AS] (system routing note in English — NOT part of the user's question)
  The user works in the AS (Aerostructures) division. Restrict your search and your
  answer to documents whose `division` metadata is "AS" or that apply to both
  divisions (shared/common documents). Ignore documents specific to the other
  division. If no relevant AS document exists, state it explicitly. Detect the
  language from the user's question below and answer in THAT language — do not let
  the English of this note change the answer's language.

  <the user's original question>
  ```

  > ⚠️ The directive is in English. Without the explicit "answer in the user's
  > language" instruction above, the model tends to treat the whole (mostly English)
  > message as an English question and replies in English even when the actual
  > question is in French — especially when the user's question is short. The
  > instruction keeps the answer language tied to the real question.

- The directive is injected **only into the outgoing payload**. In the chat UI the
  user still sees their clean question. When a past conversation is reloaded from
  history, the directive is stripped before display (`stripDivision`).

This keeps the model's filtering instruction in lock-step with the `division`
metadata column you synced, without polluting the visible conversation.

---

## Citing sources by `REF` (the "Sources consulted" panel)

The chat endpoint is the **Qualibot_Assistant** Knowledge Assistant
(`CHAT_ENDPOINT = ka-71794b8e-endpoint`). It runs its own retrieval over the index
that holds `division`, `REF`, `IDDOC`, `semantic_headers`, and streams back the answer
plus retrieval metadata. This app only **relays** what the endpoint emits.

The "Sources consulted" panel under each answer is built from that stream
(`server/services/streaming.py`). The app now **prefers the `REF` column as the source
name** when the endpoint exposes it on a `RETRIEVER` trace span:

```python
# streaming.py — RETRIEVER span extraction
ref_val   = meta.get('REF') or meta.get('ref') or item.get('REF') or item.get('ref')
title_val = ref_val or meta.get('source') or meta.get('doc_path') or doc_uri
```

So the chip/link title shows the `REF` whenever it is present in the retriever
metadata. If the endpoint instead emits citations as `url_citation` annotations
(`streaming.py:423-434`), the title is whatever the endpoint puts in `annotation.title`
— if you want the `REF` there, the Knowledge Assistant must set it as the citation
title at the source.

**How to confirm what the endpoint sends:** the app already logs each trace span's
outputs (`streaming.py:466-467`). Ask a question, then check the app logs for the
`span outputs:` lines — they show the exact metadata field names. If `REF` appears
under a different key than `REF`/`ref`, tell me the key and I'll adjust the extraction.

---

## What changed vs. the previous prompt, and why

| # | Before | After | Why |
|---|--------|-------|-----|
| 1 | "expert assistant in **Quality** document analysis" | "expert assistant for **document** analysis … quality documents **AND any other document type**" | You said the base won't contain only quality documents. The scope is broadened so the assistant doesn't wrongly assume everything is a quality doc. |
| 2 | "**ALWAYS ask if the branch is IS or AS — DON'T EVEN ANSWER IF NOT PRECISED**" | Division comes from the `[Division: …]` directive / selector. Only ask when AS and IS answers genuinely diverge. | The selector now carries the division, so forcing a question on every turn is redundant and annoying. The model still distinguishes AS/IS when it matters. |
| 3 | "identify in the **category** of the document if it is AS or IS" | "the applicable division is available in the **`division` metadata column**" | Reflects the new synced `division` column — a precise, machine-readable field instead of guessing from the category. |
| 4 | Scattered, partly lowercase bullet list with typos | Grouped into clear sections (Divisions / Sources / Answer format / When unsure / Safety) | Easier for the model to follow and for you to maintain; fixed typos ("unnecessery", "empahsize"). |
| 5 | Kept | Kept verbatim in intent | Conversation history, ref-doc + skill codes at the start, no footnotes, language matching with translation of quotes, exact citations, version/status, document-vs-metadata source, outdated-info flag, and the explicit operator safety warnings with pictograms — all preserved. |

> Note: source naming by `REF` is handled in the **code** (`streaming.py`), not in the prompt — see [Citing sources by REF](#citing-sources-by-ref-the-sources-consulted-panel).

### Behaviour summary
- **ALL**: searches both divisions; explicitly splits the answer when AS ≠ IS.
- **AS / IS**: hard-restricts to that division (+ shared docs), and says so if nothing matches.
- No more mandatory "which branch?" gate on every question — only when it's genuinely ambiguous.
