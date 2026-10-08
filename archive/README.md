# archive — code no longer used

Kept for reference only: nothing here is deployed (`archive/**` is excluded from the bundle sync and
from `deploy_qualibot.ps1`), tested (pytest only collects `tests/`) or imported by the app. The code is
as it was on its last day of use; module imports point at the old locations, so a file has to be
copied back to run. Measurements and choices: `docs/chat_vsi_tests.md`.

| Folder | What it is | Why it stopped |
|---|---|---|
| `chat_vsi_base/chat_vsi.py` | First version of the Vector Search chat (2026-10-06): one HYBRID query, top 10, the Knowledge Assistant's instructions, Claude Sonnet 4.6 | Replaced by `server/services/chat_vsi.py` (rewrite, reranker, REF/title lookups, answer rules) |
| `chat_vsi_lab/` | Every option tested between 2026-10-06 and 2026-10-08: `chat_vsi_baseline.py` (base + resilience layer), `chat_vsi_rerank.py` (reranker and all its switches: merge, context budget, noise filter…), `chat_vsi_prompts.py` (instruction sets), `chat_vsi_variants.py` (switch between engines) | The kept options were merged into `server/services/chat_vsi.py`, the others dropped (`docs/chat_vsi_tests.md` § D) |
| `chat_vsi_lab/config/rewritten_instructions/` | Instructions rewritten for the Vector Search chat (common + one scope per division); called `v2` in the lab code and in the test log | Lost to the KA instructions + answer rules |
| `chat_vsi_lab/config/answer_rules_first_draft.md` | First draft of today's `server/config/chat_vsi/answer_rules.md`; called `v3` in the lab code and in the test log | Completed (document types, citation rule) into `answer_rules.md` |
| `knowledge_assistant/` | Knowledge Assistant provisioning job (`provisioning/`), its golden-dataset builder and evaluation notebook, its load test | Chat KA removed 2026-10-08. The golden table `qualibot_eval_golden` it built is still read by `retrieval_eval` / `pairwise_answers` |
| `evaluation/` | Evaluation notebooks of the KA → VSI study: KA vs VSI golden run, replay of real questions, RAG vs agent, multilingual audit, chunking experiment (E1/E2 enrichments, still to test, `docs/chat_vsi_tests.md` § E), feedback analysis, synthetic question generator, MLflow GenAI eval of the UAT KAs | Superseded by `utils/databricks_ops/evaluation/retrieval_eval.py` and `pairwise_answers.py` |
| `scripts/` | Local scripts of the KA replacement study (see its README) | Same |
| `doc_catalog/build_doc_catalog.py` | Builder of `server/data/doc_catalog.json` from a one-off portal scrape (`intraqual_docs.jsonl`, 2026-07) | The catalog now comes from Lakebase `doc_catalog`, rewritten by the parsing pipeline every day (task `6_update_kb_metadata`); the JSON file stays only as a fallback until that table is filled |
| `parsing_sandbox/` | One-off debugging notebooks of the parsing pipeline (image rebuilds, retries, chunking tests) | Incidents closed; they still default to the old `_v1` table names |
