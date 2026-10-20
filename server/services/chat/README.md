# services/chat

The services of the chatbot outside the engine: the LLM calls, the prompt loader, the per-turn log, the MLflow traces and
the translation bridge. The answering logic is in [engine/](engine/README.md).

Parent: [services](../README.md). The route that calls these services is [routers/chat](../../routers/chat/README.md).

## Files

| Module | Content |
|---|---|
| `engine/` | Answers one question from the Vector Search index (see its README) |
| `llm.py` | The streamed answer (`stream_answer`) and the short completions (`complete`, `complete_patiently`): endpoint chain, rounds, cooldowns, continuation after a cut, concurrency cap. Holds `TIRED_MESSAGE` |
| `config.py` | Settings outside the engine: the translation bridge (switch, endpoints, language detection thresholds and noise patterns) and the MLflow export limits |
| `prompts.py` | Loads the prompt files by key (`FILES`), cached per process. Names the prompts of a turn (`keys_for_turn`) |
| `turn_log.py` | `TurnLog`: what one turn did (data, step durations, Vector Search calls, passages, issues, status) |
| `mlflow_turns.py` | Rebuilds a finished `TurnLog` as one MLflow trace, links the registered prompt versions, records a user vote |
| `translation_bridge.py` | Translation of a question in another language into English, and of the answer back. Off unless `CHAT_TRANSLATE_BRIDGE_ENABLED` is `true`; `app.yaml` sets it to `false`. Also the local language guess used by the engine (`fast_lang_guess`). Its settings are in `config.py` |
| `__init__.py` | Empty |

## Rules

- Every chat model endpoint is a GPT Luna model: the values set in `app.yaml`, and the defaults in `engine/config.py`
  and `translation_bridge.py`.
- A failed call moves to the next endpoint of the chain. Rounds of the chain wait between them. An answer cut after part
  of it was sent is continued on the next endpoint, at most twice. A failing endpoint is cooled down for all the
  turns of the process.
- Settings are read from the environment at call time, except the translation bridge switch, which is read at import.
- MLflow traces are built only from the `TurnLog`. A step that a trace shows is recorded in the `TurnLog` first.
- The app never creates the MLflow experiment: `mlflow_turns.py` looks it up and looks again after a minute when it is
  missing. Creation and prompt registration run as jobs (`utils/evaluation`).
- Scoring runs outside the turn, in `utils/evaluation/chat_scorers.py`. The trace export has a 10-second limit and
  never raises into the turn.

## See also

- [engine](engine/README.md): the answering logic.
- [prompts](../../prompts/README.md): the prompt files this package loads.
- [infra](../infra/README.md): the streaming prices, the Vector Search gate and the document catalog used here.
