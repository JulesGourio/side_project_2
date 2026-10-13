"""Step 2 variance probe: module (streamed, stream_chat_vsi) vs v0 path (non-streamed) on the 2 diverging golden cases.

Same inputs as the golden notebooks; sequential (no concurrency, so no 429). Per turn: French query, retrieved
documents, cited documents, and whether the golden document the module missed is retrieved / cited.
"""
import asyncio, json, os, subprocess, sys, time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
sys.path.insert(0, ROOT)
from server.routers.chat import _apply_citation_markers, _number_sources, _trim_history, _with_today_date  # noqa: E402
from server.services import chat_vsi                                                                     # noqa: E402
from server.services.doc_catalog import augment_sources, canon_ref                                       # noqa: E402

HOST = 'https://dbc-c623749d-731b.cloud.databricks.com'
N = 5
CASES = {'Quel est le processus de qualification pour faire de la peinture?': 'IF20016',
         'Trouve moi le template du CMP': 'NF10065'}
chat_vsi._LLM_TIMEOUT_S = 180.0      # v0 used 180 s for its non-streamed 2000-token answer


def token() -> str:
    return json.loads(subprocess.run(['databricks', 'auth', 'token', '-p', 'latecoere', '-o', 'json'],
                                     capture_output=True, text=True, check=True).stdout)['access_token']


def refs_after_post_processing(clean: str, sources: list, citations: list) -> list:
    _apply_citation_markers(clean, citations)
    sources = augment_sources(clean, sources)
    _number_sources(sources, citations)
    return sorted({canon_ref(s['title']) for s in sources if s.get('title')})


async def module_turn(messages: list, tok: str) -> dict:
    text, sources, citations, meta = [], [], [], {}
    async for chunk in chat_vsi.stream_chat_vsi(HOST, tok, 'ALL', _with_today_date(_trim_history(messages))):
        if not chunk.startswith('data: ') or chunk[6:].strip() == '[DONE]':
            continue
        o = json.loads(chunk[6:])
        if o['type'] == 'response.output_text.delta':
            text.append(o['delta'])
        elif o['type'] == 'sources':
            sources, citations = o['sources'], o['citations']
        elif o['type'] == 'metadata':
            meta = o
        elif o['type'] == 'error':
            raise RuntimeError(o)
    retrieved = [canon_ref(r) for r in meta['tool_result'].split(', ') if r]
    return {'fr_query': meta['tool_query'], 'retrieved': retrieved,
            'cited': refs_after_post_processing(''.join(text), sources, citations)}


async def v0_turn(messages: list, tok: str) -> dict:
    conv = _trim_history(messages)
    question = conv[-1]['content']
    transcript = '\n'.join(f"{m['role']}: {m['content']}" for m in conv)
    ep = chat_vsi.llm_endpoint()
    fr = (await chat_vsi._complete(HOST, tok, ep, [{'role': 'system', 'content': chat_vsi.REWRITE_PROMPT},
                                                   {'role': 'user', 'content': transcript}], 120)).strip()
    rows = await chat_vsi.retrieve(HOST, tok, chat_vsi.index_for_division('ALL'), [question, fr], 10)
    docs = chat_vsi.group_documents(rows)
    raw = await chat_vsi._complete(HOST, tok, ep, chat_vsi.build_prompt('ALL', _with_today_date([dict(m) for m in conv]), docs), 2000)
    clean, sources, citations = chat_vsi.parse_citations(raw, docs)
    return {'fr_query': fr, 'retrieved': [canon_ref(r) for r, _ in docs],
            'cited': refs_after_post_processing(clean, sources, citations)}


async def main():
    tok = token()
    for question, golden_doc in CASES.items():
        messages = [{'role': 'user', 'content': question}]
        print(f'=== {question!r} — golden document watched: {golden_doc}', flush=True)
        for engine, fn in (('module', module_turn), ('v0', v0_turn)):
            hits_r = hits_c = 0
            for i in range(1, N + 1):
                t0 = time.time()
                out = await fn(messages, tok)
                r, c = golden_doc in out['retrieved'], golden_doc in out['cited']
                hits_r += r; hits_c += c
                print(f'{engine:6s} #{i} {time.time() - t0:5.1f}s retrieved={r!s:5s} cited={c!s:5s} '
                      f'fr_query={out["fr_query"]!r}\n          retrieved={out["retrieved"]}\n          cited={out["cited"]}', flush=True)
            print(f'{engine:6s} TOTAL {golden_doc}: retrieved {hits_r}/{N}, cited {hits_c}/{N}', flush=True)


asyncio.run(main())
