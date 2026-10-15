# evaluation

Scripts that measure the chatbot and the search: the MLflow experiment and the prompt registry of the chat, the scoring of
the live traces, the retrieval and answer comparisons, the load tests, and a check of the vision endpoints. Three files
run as jobs (`create_chat_experiment.py`, `register_chat_prompts.py`, `score_chat_traces.py`). The others are a library,
a command-line tool, or notebooks that someone runs by hand.

Parent: [`utils/`](../README.md). The MLflow trace of a chat turn is written by
[`server/services/chat/mlflow_turns.py`](../../server/services/chat/mlflow_turns.py). The jobs are in
[`resources/chat_mlflow.yml`](../../resources/chat_mlflow.yml).
## Files

| File | What it does | How it runs |
|---|---|---|
| `create_chat_experiment.py` | Creates the MLflow experiment that receives one trace per chat turn, lets the app's service principal edit it, and gives `--managers` groups CAN_MANAGE | Job `Z_2_Qualibot_Create_Chat_Experiment_<target>` (Python task), or CLI with `--profile` |
| `register_chat_prompts.py` | Registers each file of `server/prompts/chat/` in the prompt registry, and writes each version as the experiment tag `chat_prompt.<key>` | Job `Z_3_Qualibot_Register_Chat_Prompts_<target>` (Python task), or CLI with `--profile` and `--schema` |
| `score_chat_traces.py` | Scores the finished traces in batches, writes the verdicts on the traces and in the table `chat_trace_scores` | Job `D_2_Qualibot_Score_Chat_Traces_<target>` (notebook, paused) |
| `chat_scorers.py` | The scorers: free ones on every trace (language, citation, degraded turn, fallback model, latency, cost), LLM judges on a sample (groundedness, fidelity, limits, relevance) | Library, imported by `score_chat_traces.py` and by `tests/test_chat_scorers.py` |
| `check_vision_endpoints.py` | Sends each image of a folder, with a question, to each endpoint, and logs ok or fail per pair | CLI: `--profile`, `--images`, `--endpoints`, `--question`, `--timeout` |
| `load_test_chat.py` | Sends real questions to the deployed app over its WebSocket, by steps of simultaneous questions | Notebook, run by hand |
| `load_test_vector_search.py` | Sends Vector Search queries alone, by steps, to find where the endpoint refuses | Notebook, run by hand |
| `retrieval_eval.py` | Checks whether the expected documents are among the passages sent to the model, without an answer | Notebook, run by hand |
| `pairwise_answers.py` | Compares a reference version (model and index) with each contender, answer by answer, judged by another model | Notebook, run by hand |

Command forms, from the repository root:

```
python utils/evaluation/check_vision_endpoints.py --profile <cli-profile> --images <folder> [--endpoints <a,b>]
python utils/evaluation/create_chat_experiment.py --profile <cli-profile> --app qualibot [--uc-schema <catalog.schema> --warehouse-id <id>] [--managers <group,group>]
python utils/evaluation/register_chat_prompts.py --profile <cli-profile> --schema <catalog.schema>
```

`check_vision_endpoints.py` exits with 0 when every request is answered, 1 when one fails, and 2 when the folder is not a
folder or when there is no image or no endpoint. Inside a job, the two experiment and prompt scripts take no `--profile`.

## Rules

- The app never creates the experiment. The job `create_chat_experiment` creates it as the job's service principal, which
  owns it.
- A prompt gets a new version only when its file changed. The `deployed` alias points to the version whose `sha` tag
  matches the file. The app reads only the experiment tag `chat_prompt.<key>`, never the registry.
- The scoring runs after the fact, in batches. It never runs inside a chat turn. The scoring job's `dry_run` parameter
  defaults to `true`.
- `register_chat_prompts.py` needs the experiment to exist. It stops with an error when it does not.
- The load tests, the retrieval evaluation and the pairwise comparison are not in any job. Their parameters are widgets.

Tests of the chat scorers and of the prompt registration are described in [`tests/README.md`](../../tests/README.md).
