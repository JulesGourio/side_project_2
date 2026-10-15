# compare

One-shot script, run by hand and never deployed: nothing in the app or in a bundle job imports or runs it. It is an offline report on a pair of local documents: the comparison pipeline (extraction, diff, image pairing, chunking) runs on them and the result is logged, with no model call. Use it to check engine changes on real documents before a deployment.
## Files

| File | Kind | What it does |
|---|---|---|
| `compare_preview.py` | CLI | Builds the text block sent to the model for an old/new pair and logs its size, the image pairs, the number of LLM calls and the warnings |

## How to run

Run from the repository root:

```
python runbooks/scripts/compare/compare_preview.py old.docx new.docx
python runbooks/scripts/compare/compare_preview.py old.pdf new.pdf --method structured --dump-diff diff.txt
python runbooks/scripts/compare/compare_preview.py old.docx new.docx --threshold 0.45   # override the similarity threshold
```

Options (argparse):

- `old`, `new`: the two documents, positional. The script stops with an error when one is missing.
- `--method {standard,structured,comparative}`: default `structured`.
- `--dump-diff FILE`: writes the full text block to `FILE`.
- `--threshold FLOAT`: overrides the similarity threshold (`COMPARE_PAIR_RATIO_THRESHOLD`, default 0.55).
- `--chunk-threshold INT`: default 60000. `--chunk-size INT`: default 45000.

## Inputs and outputs

- Input: the two local files. The file extension selects the processor (`server/services/compare/processors/factory.py`); an unsupported type stops the script.
- Output: log lines giving the file type and method, the build time, the size of the text block with the counts of MODIFIED, ADDED and REMOVED entries, the image blocks sent and the image pairs, the number of LLM calls the real run would make, and the processor warnings.
- With `--dump-diff`, the text block is also written to the given file.

## Before you run

- Run from the repository root: the script adds the repository root to `sys.path` to import the `server` package.
- The Python dependencies of the `server` package are installed, because the processors import them.
