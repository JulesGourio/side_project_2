# services/compare/processors

The file processors. Each one turns a pair of files into the messages sent to the analysis model: it extracts the text and
the images of both files, runs a diff engine, and returns a `ProcessResult` (messages, metadata, image pairs, extraction
warnings).

Parent: [compare](../README.md). The shared diff code is in [`_diff_engines/`](_diff_engines/README.md), and the DOCX
processor in [`docx/`](docx/README.md).

## Files

| Module | Content |
|---|---|
| `base.py` | `ProcessMetadata`, `ProcessResult`, and the abstract `BaseProcessor.build_messages` |
| `factory.py` | `EXTENSION_MAP` (extension to file type), `get_processor` (method from the environment or from the request), `file_type_of`, `extract_document_text` (one document, for the summary), `prepare_image_content`, `diff_truncation_warnings` |
| `text_diff.py` | `TextDiffProcessor`: the template of the text processors. Subclasses give the extraction and the wording of the notes |
| `pdf.py` | PDF (`PDFProcessor`, PyMuPDF): text by block with page tags, merge of paragraphs split by a page break, table rows, embedded images deduplicated by hash |
| `docx/` | DOCX and DOC (`DocxProcessor`) |
| `pptx.py` | PPTX and PPT (`PptxProcessor`): slide text and images, tagged by slide |
| `xml.py` | XML (`XMLProcessor`): text of the elements, numbered items, no images |
| `excel.py` | XLSX and XLS (`ExcelProcessor`, a `BaseProcessor`): cell diff keyed by the first column |
| `image.py` | Images (`ImageProcessor`, a `BaseProcessor`): both images sent to a vision model, after resizing and JPEG conversion |
| `versions.yml` | The methods of each file type: id, label, description, default, hidden. Served by `GET /api/config/processors` |
| `_diff_engines/` | The diff engines, their prompts and the image hashing, shared by the processors |

## The three methods

- `standard`: paragraph diff, Markdown bullet output (`SYSTEM_PROMPT_STANDARD`).
- `structured`: paragraph diff, JSON array output (`SYSTEM_PROMPT_STRUCTURED`).
- `comparative`: section diff, with the system prompt given by the caller (`COMPARE_ANALYSIS_SYSTEM_PROMPT`).

An unknown method is handled as `standard`. The Excel and image processors and the text template all do this.

## Rules

- `build_messages` is synchronous. Callers run it in a thread.
- The text processors cut the diff at `COMPARE_MAX_DIFF_CHARS` (`truncate_diff`). The router then tells the user that the
  end of the report is missing.
- The extraction warnings (a near-empty text layer, documents that do not match) reach the user as well as the model.
- The text processors add a visual block for the images they found. The XML processor has no images.
- The Excel diff matches rows on their first column. A column of formulas or dates that changes in more than half of
  the matched rows (sheets of at least 20 rows) is left out as recalculated and listed in the report; a column of
  typed-in values is always compared. A sheet present in one workbook only is reported with its columns and its first
  30 rows. When the first
  column is not a unique key, the diff is a line-level unified diff instead.
- Extensions are read in lower case: `.PDF` is handled like `.pdf`.
- Known limit: tables inside the headers and footers of a DOCX file are not read (their paragraphs are). The version and
  date of a document are in paragraphs, so the comparison keeps them. Those tables are mostly revision tables and
  boilerplate; reading them is a planned extension that can wait.


## See also

- [_diff_engines](_diff_engines/README.md): the diff engines.
- [docx](docx/README.md): the DOCX extraction and page estimate.
- [compare](../README.md): the callers of the processors.
