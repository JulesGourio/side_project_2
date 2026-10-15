# services/infra/pdf_report

The PDF layout of the exports: chatbot answers, chatbot conversations, change summaries and document summaries share one
layout. Markdown goes in, an A4 PDF comes out (fpdf2, pure Python).

Parent: [infra](../README.md). The route that calls it is [routers/exports.py](../../../routers/exports.py).

## Files

| Module | Content |
|---|---|
| `__init__.py` | `build_report_pdf` (kind, title, sections, metadata) and `build_markdown_pdf` (one Markdown body, for the comparison exports). Re-exports the data model and the parsing helpers |
| `common.py` | `KIND_LABELS` (the label above the title of each kind), the palette, the page geometry, the `Source` and `Section` data classes, `FONT_DIR` |
| `markdown.py` | Markdown to block tokens and inline spans. `clean_text` keeps only the characters the font can draw |
| `render.py` | The pages: the running header and footer, the cover block, and one writer method per block type (headings, paragraphs, list items, quotes, code, tables, rules, the question box, the sources) |

## Rules

- The font is Inter (Regular, SemiBold, Italic, SemiBold Italic), read from [`server/assets/fonts`](../../../assets/fonts).
  The app container has no system fonts.
- A character the font lacks, emoji included, is dropped by `clean_text`.
- A `[n]` marker in the text becomes a superscript link to source `n`, when the section lists that source with a URL.
- The route rejects a kind that is not in `KIND_LABELS`. The builder falls back to the `report` label for any other value.
- A section can open with a question box and close with its numbered sources.
- The date on the cover is the day of the export, in UTC.

## See also

- [infra](../README.md): the other shared services.
- [routers/exports.py](../../../routers/exports.py): the routes that return these PDFs.
