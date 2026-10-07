# scripts — KA replacement tests (2026-10-06)

Code written during the KA replacement study. See `docs/dev-conception.md` for the results.

All scripts target the dev workspace `dbc-c623749d-731b`. Except `app_chat_turn.py`, they don't touch the app.

| Script | Where it runs | What it does |
|---|---|---|
| `capture_ka_stream.py` | Local (CLI profile `latecoere`) | One real streaming call to the KA ALL endpoint with the app's request body; saves the raw SSE and prints the event sequence, annotations and trace spans |
| `vsi_test.py` | Local (CLI profile `latecoere`) | First test without the KA: Vector Search HYBRID → `claude-sonnet-4-6` with the live KA instructions → `[n]` markers → the app's own post-processing (`chat.py`, `doc_catalog.py`). Single question. |
| `vs_rank_check.py` | Local (CLI profile `latecoere`) | Rank of given documents in Vector Search results per query wording (EN / FR), to tell retrieval gaps from generation gaps |
| `qualibot_vsi_vs_ka.py` | Workspace notebook, serverless job | KA vs VSI v0 on the 5 questions prefilled in the project. KA answers are the reference. MLflow experiment `/Users/mehdi.lamrani@databricks.com/qualibot-vsi-vs-ka`. |
| `qualibot_golden_ka_vs_vsi.py` | Workspace notebook, serverless job | KA and VSI v0 both scored against the golden dataset `dev_landingzone.qualibot.qualibot_eval_golden` (21 cases): `Correctness`, `ExpectationsGuidelines`, `golden_doc_recall`. Same MLflow experiment. |
| `check_vsi_instructions.py` | Local (CLI profile `latecoere`) | `server/config/chat_vsi/instructions_*.md` vs the live KA instructions (AS authoring note excluded); exit 0 if identical |
| `capture_vsi_raw_answers.py` | Local (CLI profile `latecoere`) | The integrated module's raw answers (`[n]` markers, real stream chunking) on the golden cases → `tests/fixtures/chat_vsi_raw_answers.json` |
| `qualibot_golden_eval.py` | Workspace notebook, serverless job | Golden evaluation of the app's own engines, `stream_chat()` (KA) and/or `stream_chat_vsi()` (widgets `engines`, `run_tag`). Same scorers and MLflow experiment. |
| `app_chat_turn.py` | Local | One Chat turn over the app WebSocket (local dev server or deployed app), printed as a text transcript: markers leaked in the stream, ⟦n⟧ and numbered sources, or the error shown |

## Running the notebooks

The notebooks import the app code from `/Workspace/Shared/qualibot-custom` (the synced copy of this repo).

```bash
P="-p latecoere"; D=/Users/mehdi.lamrani@databricks.com/qualibot-eval
databricks $P workspace import $D/qualibot_golden_ka_vs_vsi --file scripts/qualibot_golden_ka_vs_vsi.py \
  --format SOURCE --language PYTHON --overwrite
databricks $P jobs submit --no-wait --json "{\"run_name\":\"qualibot-golden-ka-vs-vsi\",\"tasks\":[{\"task_key\":\"eval\",\"notebook_task\":{\"notebook_path\":\"$D/qualibot_golden_ka_vs_vsi\",\"source\":\"WORKSPACE\"}}]}"
```
