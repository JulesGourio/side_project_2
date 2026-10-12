"""One Chat turn over the app's WebSocket, the way the front sends it, with a text transcript of the result.

Works against the local dev server (Vite proxy) or the deployed app (OAuth token of the CLI profile).
Prints whether a `[n]` marker leaked into the stream, the ⟦n⟧ markers and numbered sources of the `done`
message, or the error shown to the user.

    .venv/bin/python scripts/app_chat_turn.py <base_url> <ws_path> <division> "<question>"
    # deployed:  https://qualibot-custom-2865348338307293.aws.databricksapps.com /api/chat/ws AS "..."
    # local:     http://localhost:3000 /api/chat/ws ALL "..."
"""
import asyncio, json, re, subprocess, sys, time

import websockets

PROFILE = 'latecoere'
MARKER = re.compile(r'\[\d+\]|\[$')


def token() -> str:
    return json.loads(subprocess.run(['databricks', 'auth', 'token', '-p', PROFILE, '-o', 'json'],
                                     capture_output=True, text=True, check=True).stdout)['access_token']


import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("app_chat_turn")


async def turn(base_url: str, ws_path: str, division: str, question: str) -> None:
    ws_url = re.sub(r'^http', 'ws', base_url.rstrip('/')) + ws_path
    headers = {} if 'localhost' in base_url else {'Authorization': f'Bearer {token()}'}
    t0 = time.time()
    async with websockets.connect(ws_url, additional_headers=headers, max_size=None) as ws:
        await ws.send(json.dumps({'messages': [{'role': 'user', 'content': question}], 'division': division,
                                  'session_id': f'check-{division.lower()}-{int(t0)}'}))
        deltas, first = [], None
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=240))
            if m['type'] == 'delta':
                deltas.append(m['delta'])
                first = first or time.time() - t0
            elif m['type'] == 'done':
                srcs = m.get('sources') or []
                numbered = [s for s in srcs if s.get('n')]
                logger.info(f'[{ws_path} {division}] OK deltas={len(deltas)} first_delta={first and round(first, 1)}s '
                      f'total={time.time() - t0:.1f}s markers_in_stream={sum(1 for d in deltas if MARKER.search(d))} '
                      f'⟦n⟧_in_done={m["content"].count("⟦")} numbered_sources={len(numbered)} '
                      f'intraqual_urls={all("intraqual" in (s.get("url") or "") for s in numbered)}')
                logger.info("%s", " ".join(str(x) for x in ('   sources:', [(s.get('n'), s['title']) for s in srcs][:12],)))
                return
            elif m['type'] == 'error':
                logger.info(f'[{ws_path} {division}] ERROR after {time.time() - t0:.1f}s: {m["error"]}')
                return


asyncio.run(turn(*sys.argv[1:5]))
