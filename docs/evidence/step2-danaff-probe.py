"""Step 2 probe: does the streamed module invent the DANAFF expansion more often than the non-streamed v0 path?

Same inputs as the golden; sequential. The golden expects "no definition in the documents"; the failure mode
seen in the runs is an invented expansion ("Demande d'Autorisation ..."), detected here by regex.
"""
import asyncio, json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from server.routers.chat import _trim_history, _with_today_date      # noqa: E402
from server.services import chat_vsi                                   # noqa: E402

HOST = 'https://dbc-c623749d-731b.cloud.databricks.com'
N = 5
QUESTION = "c'est quoi une DANAFF"
INVENTED = re.compile(r"Demande d.Autorisation|DANAFF\*{0,2}\s*\(", re.I)
chat_vsi._LLM_TIMEOUT_S = 180.0


def token() -> str:
    return json.loads(subprocess.run(['databricks', 'auth', 'token', '-p', 'latecoere', '-o', 'json'],
                                     capture_output=True, text=True, check=True).stdout)['access_token']


async def module_answer(messages, tok):
    text, meta = [], {}
    async for chunk in chat_vsi.stream_chat_vsi(HOST, tok, 'ALL', _with_today_date(_trim_history(messages))):
        if chunk.startswith('data: ') and chunk[6:].strip() != '[DONE]':
            o = json.loads(chunk[6:])
            if o['type'] == 'response.output_text.delta':
                text.append(o['delta'])
            elif o['type'] == 'metadata':
                meta = o
            elif o['type'] == 'error':
                raise RuntimeError(o)
    return meta['tool_query'], ''.join(text)


async def v0_answer(messages, tok):
    conv = _trim_history(messages)
    ep = chat_vsi.llm_endpoint()
    transcript = '\n'.join(f"{m['role']}: {m['content']}" for m in conv)
    fr = (await chat_vsi._complete(HOST, tok, ep, [{'role': 'system', 'content': chat_vsi.REWRITE_PROMPT},
                                                   {'role': 'user', 'content': transcript}], 120)).strip()
    rows = await chat_vsi.retrieve(HOST, tok, chat_vsi.index_for_division('ALL'), [conv[-1]['content'], fr], 10)
    docs = chat_vsi.group_documents(rows)
    raw = await chat_vsi._complete(HOST, tok, ep, chat_vsi.build_prompt('ALL', _with_today_date([dict(m) for m in conv]), docs), 2000)
    return fr, chat_vsi.parse_citations(raw, docs)[0]


async def main():
    tok = token()
    messages = [{'role': 'user', 'content': QUESTION}]
    print(f'=== {QUESTION!r} — failure mode watched: invented expansion', flush=True)
    for engine, fn in (('module', module_answer), ('v0', v0_answer)):
        invented = 0
        for i in range(1, N + 1):
            t0 = time.time()
            fr, answer = await fn(messages, tok)
            hit = bool(INVENTED.search(answer))
            invented += hit
            print(f'{engine:6s} #{i} {time.time() - t0:5.1f}s invented={hit!s:5s} fr_query={fr!r}\n'
                  f'          {answer[:160]!r}', flush=True)
        print(f'{engine:6s} TOTAL invented expansion: {invented}/{N}', flush=True)


asyncio.run(main())
