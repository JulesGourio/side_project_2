"""Check that server/config/chat_vsi/instructions_*.md match the live KA instructions.

The only allowed difference is the stray authoring note at the end of the live AS
instructions ("▎ Note : …"), which is deliberately not copied.
Run locally (CLI profile `latecoere`): python3 scripts/check_vsi_instructions.py
"""
import json, os, subprocess, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
KAS = {'all': '4d15cb32-1edb-4f86-aa7c-e6c2e91a9002',   # qualibot_ALL_v2
       'as': '2ef8a9ac-bf46-4b92-97a1-f2f395eab2f4',    # qualibot_AS_v2
       'is': '710526e7-42ef-4e3b-b9cb-9887a6c12aaa'}    # qualibot_IS_v2
STRAY_NOTE = '▎ Note'

ok = True
for div, ka_id in KAS.items():
    live = json.loads(subprocess.run(
        ['databricks', 'knowledge-assistants', 'get-knowledge-assistant', f'knowledge-assistants/{ka_id}',
         '-p', 'latecoere', '-o', 'json'], capture_output=True, text=True, check=True).stdout)['instructions']
    expected = live[:live.index(STRAY_NOTE)].rstrip() if STRAY_NOTE in live else live
    with open(os.path.join(ROOT, 'server', 'config', 'chat_vsi', f'instructions_{div}.md'), encoding='utf-8') as f:
        repo = f.read().rstrip('\n')
    same = repo == expected
    ok &= same
    note = ' (live stray note excluded)' if STRAY_NOTE in live else ''
    print(f'{div}: {"IDENTICAL" if same else "DIFFERENT"} — repo {len(repo)} chars, live {len(live)} chars{note}')
sys.exit(0 if ok else 1)
