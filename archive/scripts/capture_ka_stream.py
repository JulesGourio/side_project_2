"""Capture one real streaming KA response, with the same request body as the app, and summarize it.

Used on 2026-10-06 to document the KA output contract (docs/ka-black-box-io-contract.md §3).
Run locally (CLI profile `latecoere`): python3 scripts/capture_ka_stream.py [output.txt]
"""
import json, re, subprocess, sys
import httpx

HOST = 'https://dbc-c623749d-731b.cloud.databricks.com'
ENDPOINT = 'ka-4d15cb32-endpoint'          # qualibot_ALL_v2
QUESTION = '[Date: 2026-10-06]\n\nQuelles procédures parlent de qualification CND ?'
OUT = sys.argv[1] if len(sys.argv) > 1 else 'ka_stream.txt'

TOKEN = json.loads(subprocess.run(['databricks', 'auth', 'token', '-p', 'latecoere', '-o', 'json'],
                                  capture_output=True, text=True, check=True).stdout)['access_token']
body = {'input': [{'role': 'user', 'content': QUESTION}], 'stream': True,
        'databricks_options': {'return_trace': True}}          # exactly what stream_chat() sends

with httpx.stream('POST', f'{HOST}/serving-endpoints/{ENDPOINT}/invocations', timeout=180, json=body,
                  headers={'Authorization': f'Bearer {TOKEN}'}) as resp, open(OUT, 'w', encoding='utf-8') as f:
    print('HTTP', resp.status_code)
    for line in resp.iter_lines():
        f.write(line + '\n')

# ── Summary ──────────────────────────────────────────────────────────────────
lines = [l[6:].strip() for l in open(OUT, encoding='utf-8') if l.startswith('data: ')]
objs = [json.loads(d) for d in lines if d != '[DONE]']

seq = []
for o in objs:
    t = o.get('type', '?')
    if seq and seq[-1][0] == t:
        seq[-1][1] += 1
    else:
        seq.append([t, 1])
print('\nEvent sequence:')
for t, c in seq:
    print(f'  {t} x{c}')

text, anns = '', []
for o in objs:
    if o.get('type') == 'response.output_text.delta':
        text += o.get('delta', '')
    elif o.get('type') == 'response.output_text.annotation.added':
        anns.append((len(text), o['annotation'].get('title', '')))
FOOTNOTE = re.compile(r'\[\^[^\]]+\]')
print(f'\nText: {len(text)} chars | footnote markers in deltas: {len(FOOTNOTE.findall(text))}')
print('Annotations (position = text length on arrival):')
for pos, title in anns:
    print(f'  pos {pos}: {title}')

done = next(o for o in objs if o.get('type') == 'response.output_item.done')
item_text = done['item']['content'][0]['text']
trace = done['databricks_output']['trace']
n_refs = len(re.findall(r'\[\^[^\]]+\](?!:)', item_text))
print(f'\noutput_item.done: item text {len(item_text)} chars ({n_refs} footnote refs) | trace_id {trace["info"]["trace_id"]}')
for s in trace['data']['spans']:
    a = s.get('attributes', {})
    extra = ''
    if a.get('mlflow.spanType', '').strip('"') == 'RETRIEVER':
        docs = json.loads(a['mlflow.spanOutputs'])
        extra = f' -> {len(docs)} passages: ' + ', '.join(d['page_content'].split('|')[0][9:].strip() for d in docs)
    print(f'  span {s["name"]} {a.get("mlflow.spanType")}{extra}')
