"""Capture raw Chat VSI answers (with their [n] markers and the real stream chunking) on the golden dataset.

Output: tests/fixtures/chat_vsi_raw_answers.json, used by tests/test_chat_vsi.py to check that the
streaming citation parser gives exactly the same text and citations as the whole-text parse
(plan step 2, "équivalence"). Runs the module's own pipeline pieces against the real services.

Run locally from the repo root, with the venv (CLI profile `latecoere`):
    .venv/bin/python scripts/capture_vsi_raw_answers.py
"""
import asyncio, json, os, subprocess, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

from server.routers.chat import _trim_history, _with_today_date                       # noqa: E402
from server.services import chat_vsi                                                    # noqa: E402
from server.services.streaming import stream_analysis                                   # noqa: E402

PROFILE = 'latecoere'
HOST = 'https://dbc-c623749d-731b.cloud.databricks.com'
WAREHOUSE = '13eff4a6095513cb'
GOLDEN = 'dev_landingzone.qualibot.qualibot_eval_golden'
OUT = os.path.join(ROOT, 'tests', 'fixtures', 'chat_vsi_raw_answers.json')


def token() -> str:
    return json.loads(subprocess.run(['databricks', 'auth', 'token', '-p', PROFILE, '-o', 'json'],
                                     capture_output=True, text=True, check=True).stdout)['access_token']


def golden_cases() -> list:
    body = json.dumps({'warehouse_id': WAREHOUSE, 'wait_timeout': '50s',
                       'statement': f'SELECT inputs FROM {GOLDEN} ORDER BY dataset_record_id'})
    out = json.loads(subprocess.run(['databricks', 'api', 'post', '/api/2.0/sql/statements', '-p', PROFILE,
                                     '--json', body, '-o', 'json'], capture_output=True, text=True, check=True).stdout)
    return [json.loads(row[0])['messages'] for row in out['result']['data_array']]


async def capture(messages: list, tok: str) -> dict:
    conv = chat_vsi._clean_history(_with_today_date(_trim_history(messages)))
    question = chat_vsi._without_date(conv[-1]['content'])
    endpoint = chat_vsi.llm_endpoint()
    fr_query = await chat_vsi.search_query_fr(HOST, tok, endpoint, conv[:-1] + [{'role': 'user', 'content': question}])
    rows = await chat_vsi.retrieve(HOST, tok, chat_vsi.index_for_division('ALL'),
                                   [question] + ([fr_query] if fr_query else []), chat_vsi.num_results())
    documents = chat_vsi.group_documents(rows)
    deltas = []
    async for chunk in stream_analysis(HOST, tok, endpoint, chat_vsi.build_prompt('ALL', conv, documents),
                                       max_tokens=2000, thinking_budget=0, temperature=0.0, operation='Chat'):
        if chunk.startswith('data: ') and chunk[6:].strip() != '[DONE]':
            event = json.loads(chunk[6:])
            if event.get('type') == 'response.output_text.delta':
                deltas.append(event['delta'])
            elif event.get('type') == 'error':
                raise RuntimeError(event)
    return {'question': question, 'documents': [[ref, {'url': d['url']}] for ref, d in documents], 'deltas': deltas}


async def main():
    tok = token()
    cases = golden_cases()
    captured = []
    for i, messages in enumerate(cases, 1):
        item = await capture(messages, tok)
        markers = sum(d.count('[') for d in item['deltas'])
        print(f'[{i}/{len(cases)}] {len(item["deltas"])} deltas, {len(item["documents"])} documents, '
              f'{markers} "[" — {item["question"][:70]!r}')
        captured.append(item)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(captured, f, ensure_ascii=False, indent=1)
    print(f'wrote {OUT} ({len(captured)} answers)')


asyncio.run(main())
