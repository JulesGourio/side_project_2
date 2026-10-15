# services/compare/processors/_diff_engines

The diff engines shared by every processor: the alignment of the text (paragraphs or sections), the cleanup of its output,
the detection of text that moved, the image hashing and pairing, and the system prompts of the two text methods.

Parent: [processors](../README.md).

## Files

| Module | Content |
|---|---|
| `__init__.py` | Re-exports the names the processors use |
| `engines.py` | `paragraph_semantic_diff` (the primary engine: aligns similar paragraphs and shows the changes inline) and `section_canonical_diff` (a unified diff grouped by heading) |
| `text.py` | Structural tags (`[Page N]`, `[Para N]`), canonical forms, modal verbs, the substantive-change tests, `inline_word_diff`, `truncate_diff`, `extraction_warnings` |
| `collapse.py` | Merges entries: running headers and footers, the same change repeated on every page, a text cut differently in two places, a restyle repeated across the document |
| `relocation.py` | Segmentation artifacts of the extraction, text that moved instead of disappearing (`_tag_relocated`), alignment of duplicate blocks |
| `images.py` | `dhash`, `ahash`, `hash16`. Pairs the images of both files as MODIFIED, ORIENTATION CHANGED, REMOVED or ADDED, and builds the image blocks for the model |
| `prompts.py` | `SYSTEM_PROMPT_STANDARD` and `SYSTEM_PROMPT_STRUCTURED` |

## Rules

- The inline markup of a diff is `~~removed~~` for the deleted words and `**added**` for the new ones. The analysis prompts
  describe this markup.
- The thresholds of the engines are environment variables read at import: `COMPARE_PAIR_RATIO_THRESHOLD`,
  `COMPARE_ARTIFACT_MAX_WORDS`, `COMPARE_ARTIFACT_MAX_POS_DRIFT`, `COMPARE_ARTIFACT_DENSE_LABEL_MIN`,
  `COMPARE_BOILERPLATE_MIN_PAGES`, `COMPARE_SYSTEMIC_SUB_MIN`, `COMPARE_WORD_LEVEL_CHECK`, `COMPARE_MAX_DIFF_CHARS`.
- A change between two modal verbs is substantive when the verbs fall in different obligation groups (mandatory,
  permissive, advisory). A change inside one group is not.
- Two images are the same when their hashes are close. A finer 16 by 16 hash confirms the identity of a pair.

## See also

- [processors](../README.md): the processors that call these engines.
