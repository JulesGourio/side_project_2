# services/chat/engine

The chat engine. It answers a question from the Vector Search index: it turns the question into French and English search
queries, runs the searches, names the documents, builds the prompt, and streams the answer with numbered citations.

Parent: [chat](../README.md). The route that calls it is [routers/chat/ws.py](../../../routers/chat/ws.py). The prompts it
loads are in [prompts](../../../prompts/README.md).

## Files

| Module | Content |
|---|---|
| `__init__.py` | The public names of the package (`stream_chat`, `retrieve_for_turn`, `settings`, ...) |
| `types.py` | The shapes of the dicts the engine passes around: `SearchRow`, `Hit`, `PassageRecord`, `DocumentPassages` |
| `config.py` | Settings read from the `CHAT_*` environment at call time. The division filter (`ALL`, `AS`, `IS`). `settings()`, recorded on each turn. The system instructions (`division_prompt`, `load_instructions`, cached). The patterns that strip company and document-type words from a rewrite (`NOISE_PHRASES`) |
| `queries.py` | Translation of an opening message (`translate_query`), the bilingual rewrite (`rewrite_queries`), `split_bilingual`, `strip_added_noise`, `opening_language` |
| `search.py` | The Vector Search calls of a turn: reranked and raw HYBRID queries, merge by best rank, lookups filtered by REF, and the early search of the question (`start_question_search`) |
| `retrieval.py` | `retrieve_for_turn`: everything before the answer. Named documents, one language per document, the passage records |
| `answer.py` | `stream_chat`: the answer stream. `build_prompt`, the revision note of each document, the language reminder, the answer's log record |
| `citations.py` | `CitationStreamParser`: strips the `[n]` markers from the stream and records their positions. `parse_citations` is the whole-text version |

## How a turn runs

1. `retrieve_for_turn` translates an opening message in French or English when `CHAT_TRANSLATE_ONLY` is on. Otherwise it
   runs the bilingual rewrite, and the reranked search of the question as asked starts at once, alongside the rewrite.
2. The queries are the question and its French and English versions, without duplicates. Raw searches run on the
   queries named by `CHAT_RAW_ON`.
3. The REFs named in the question and in the earlier messages are looked up with a REF filter, using the catalog.
4. `one_language_per_document` keeps the best-ranked language variant of each document.
5. `build_prompt` numbers the documents `[1]`, `[2]`... and gives each one its revision and date. The passages come before
   the question. `stream_answer` (in `services/chat/llm.py`) streams the model's answer. `CitationStreamParser` replaces
   the `[n]` markers by `⟦n⟧` positions and numbers the sources in order of first appearance.

## Rules

- Every model the engine calls is a `CHAT_*` endpoint. The defaults are GPT Luna models.
- A query that fails is dropped when another query answered. `ChatVsiError` is raised only when every query failed.
- Every passage retrieved is recorded on the turn, kept or set aside. A passage set aside has a reason: `other_language`
  or `over_cap`.
- The rewrite drops the company and document-type words that the conversation does not use (`strip_added_noise`).
- The prompt gets the date sent with the question. The engine removes that `[Date: …]` prefix before searching.
- The sources shown as chips are built from the `[n]` markers and the catalog. The system prompt (`base.md`) asks the model to end
  an answer with a table of the documents it used (REF, title, version, status, division), written from the documents list.
- Documents are filtered by `division` in the single index. `ALL` applies no filter.
- The citation parser holds back a partial marker at the end of a chunk until the next chunk decides what it is.

## See also

- [chat](../README.md): the other chat services.
- [prompts](../../../prompts/README.md): the prompt files.
- [infra](../../infra/README.md): the Vector Search gate, the document catalog and the streaming helpers.
