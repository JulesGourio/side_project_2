# services/compare/processors/docx

The DOCX and DOC processor: the text of a Word file in document order, its embedded images, and an estimate of its page
numbers.

Parent: [processors](../README.md). The template it extends is [text_diff.py](../text_diff.py), and the shared engines are
in [_diff_engines](../_diff_engines/README.md).

## Files

| Module | Content |
|---|---|
| `__init__.py` | `DocxProcessor` (a `TextDiffProcessor`) and `_load_docx`, which reads a `.doc` file through `extract.py` and a `.docx` file directly |
| `extract.py` | `_extract_docx`: the lines in document order, tagged `[Page N, Para M]`, and the images with their hashes. `_extract_doc` for `.doc` files |
| `ooxml.py` | Reads the WordprocessingML elements: paragraphs, tables, text boxes, VML arrows, footnotes. Skips paragraphs in the table-of-contents styles |
| `pages.py` | The page estimate. Without a rendering engine the page breaks and the last-rendered page markers give the page of each block (`_assign_pages`) |

## Rules

- Tracked changes are read as accepted: a paragraph's text is what the reader sees with every change accepted.
- The page numbers are estimates. The `[Page N]` tags they produce are not the printed page numbers.
- A `.doc` file is read through `services/compare/conversion.py` (antiword, then olefile).

## See also

- [processors](../README.md): the processors and the method table.
- [conversion](../../conversion.py): the `.doc` text extraction.
