# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot : stress test du chat
# MAGIC
# MAGIC Simule des utilisateurs qui posent des questions au chat, mesure ce qu'ils vivent (temps avant le premier mot,
# MAGIC temps total, erreurs, refus) et trace la charge dans le temps. Réglé par défaut pour **PROD**.
# MAGIC
# MAGIC **Deux cibles** (`cible`)
# MAGIC - `app` : les questions passent par l'app déployée, exactement comme le navigateur (WebSocket `/api/chat/ws`,
# MAGIC   même en-tête `Origin`, même historique envoyé à chaque tour). Tout est réel : l'instance de l'app, sa file
# MAGIC   d'attente, la limite par utilisateur, la traduction, Vector Search, les modèles, l'enregistrement en Lakebase et
# MAGIC   les traces MLflow.
# MAGIC - `moteur` : le code de l'app déployée (`dossier_app`) est chargé dans ce notebook et chaque question suit les
# MAGIC   mêmes étapes que la route WebSocket (`server/routers/chat/ws.py`) : nettoyage et fenêtre de l'historique, date,
# MAGIC   traduction de la question, `stream_chat` (réécriture, recherche, réponse, mêmes limites de concurrence),
# MAGIC   marqueurs de citation, sources du catalogue, traduction retour. Seuls l'enregistrement en Lakebase et la trace
# MAGIC   MLflow sont sautés : rien n'est écrit dans l'historique de l'app. La limite par utilisateur de l'app peut être
# MAGIC   appliquée à chaque utilisateur simulé (`limite_par_utilisateur`).
# MAGIC
# MAGIC **Appels LLM** (`appels_llm`, cible `moteur` seulement ; la cible `app` fait toujours le tour complet)
# MAGIC - `complet` : le tour entier, réponse comprise.
# MAGIC - `recherche` : tout jusqu'aux passages envoyés au modèle (`retrieve_for_turn` : traduction ou réécriture LLM,
# MAGIC   Vector Search, recherche par REF et citations exactes), sans générer la réponse. Peu coûteux.
# MAGIC - `aucun` : aucun appel LLM. La recherche part de la question telle que posée, comme l'app quand la réécriture
# MAGIC   échoue. Mesure Vector Search seul.
# MAGIC
# MAGIC **Profils de charge** (`profil`)
# MAGIC - `vague` : `utilisateurs` questions envoyées toutes au même instant.
# MAGIC - `realiste` : `utilisateurs` personnes arrivent pendant `duree_s` secondes (arrivées au hasard, loi de Poisson,
# MAGIC   ou régulières). Chacune mène une conversation de `tours_par_session` questions et lit chaque réponse pendant
# MAGIC   `reflexion_s` secondes en moyenne avant la suivante.
# MAGIC - `rejeu` : rejoue une vraie période de la table des messages (`rejeu_jour`, `rejeu_fenetre_min`) : mêmes
# MAGIC   conversations, mêmes questions, mêmes écarts entre questions, accélérés `acceleration` fois.
# MAGIC
# MAGIC **Sécurités** : `lancer = non` (défaut) affiche seulement le plan (nombre de tours, pic de requêtes par minute,
# MAGIC refus attendus par la limite de l'app) sans rien envoyer. `max_tours` plafonne le nombre de questions.
# MAGIC `arret_erreurs_pct` arrête d'envoyer de nouvelles questions quand trop des 20 dernières ont échoué. En cible `app`,
# MAGIC `nettoyer = oui` supprime à la fin (suppression logique, comme le bouton de l'app) les conversations créées.
# MAGIC
# MAGIC **Ce que coûte et laisse un run**
# MAGIC - `complet` : par question, une traduction ou réécriture, une réponse et parfois une traduction de la question et
# MAGIC   de la réponse, sur les mêmes endpoints que les vrais utilisateurs (`databricks-gpt-6-luna`, secours
# MAGIC   `databricks-gpt-5-6-luna`). Une saturation pendant le test les touche aussi.
# MAGIC - cible `app` : chaque question est enregistrée sous votre utilisateur (`chat_sessions`, `chat_messages`,
# MAGIC   `chat_turns`, traces MLflow). Les conversations sont retrouvables par `session_id` dans la table de résultats.
# MAGIC - `table_resultats` : une ligne par question, avec le `run_id` du run.
# MAGIC
# MAGIC **Résultats** : indicateurs clés, chronologie de chaque question, charge et latence dans le temps, distribution des
# MAGIC temps, détail des étapes (cible `moteur`), erreurs par message, historique des runs.

# COMMAND ----------

dbutils.widgets.removeAll()
dbutils.widgets.text('dossier_app', '/Workspace/.bundle/qualibot/qualibot-prod/files')
APP = dbutils.widgets.get('dossier_app').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml websockets matplotlib "databricks-sdk>=0.102"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Paramètres
# MAGIC
# MAGIC | Paramètre | Défaut | Sens |
# MAGIC |---|---|---|
# MAGIC | `dossier_app` | `/Workspace/.bundle/qualibot/qualibot-prod/files` | code de l'app déployée : sa configuration (`app.yaml`, `target_config.env`) et, en cible `moteur`, son moteur |
# MAGIC | `lancer` | `non` | `non` = plan seul, rien n'est envoyé ; `oui` = lance le test |
# MAGIC | `cible` | `app` | `app` = l'app déployée ; `moteur` = le code de l'app dans ce notebook (voir en haut) |
# MAGIC | `appels_llm` | `complet` | `complet`, `recherche` ou `aucun` (cible `moteur` seulement) |
# MAGIC | `profil` | `realiste` | `vague`, `realiste` ou `rejeu` |
# MAGIC | `utilisateurs` | `20` | `vague` : questions envoyées d'un coup ; `realiste` : personnes qui arrivent. Ignoré par `rejeu` |
# MAGIC | `duree_s` | `60` | `realiste` : fenêtre d'arrivée des personnes, en secondes |
# MAGIC | `arrivees` | `poisson` | `realiste` : `poisson` = au hasard comme de vrais utilisateurs ; `reguliere` = une arrivée tous les `duree_s / utilisateurs` |
# MAGIC | `tours_par_session` | `1-3` | questions par conversation : un nombre (`2`) ou un intervalle tiré au hasard (`1-3`). `vague` avec `1` = une seule question chacun |
# MAGIC | `reflexion_s` | `20` | temps moyen de lecture d'une réponse avant la question suivante (tiré entre 0,5 et 1,5 fois cette valeur) |
# MAGIC | `rejeu_jour` | `auto` | `rejeu` : `auto` = l'heure la plus chargée de la table des messages ; sinon début de la période, `AAAA-MM-JJ HH:MM` (UTC) |
# MAGIC | `rejeu_fenetre_min` | `60` | `rejeu` : durée de la période rejouée, en minutes |
# MAGIC | `acceleration` | `1` | `rejeu` : 1 = vitesse réelle, 4 = la période passe 4 fois plus vite (4 fois plus de charge) |
# MAGIC | `source_questions` | `sessions` | `sessions` = vraies conversations de `table_messages` (suites de questions réelles) ; `golden` = questions de `table_golden` ; `mix` = les deux |
# MAGIC | `division` | `reelle` | `reelle` = la division de la question d'origine ; `ALL`, `AS` ou `IS` = forcée pour toutes |
# MAGIC | `timeout_s` | `300` | une question sans fin après ce délai compte en `timeout` |
# MAGIC | `max_tours` | `200` | plafond de questions envoyées pendant le run |
# MAGIC | `arret_erreurs_pct` | `50` | plus de questions nouvelles quand plus de ce % des 20 dernières a échoué (0 = jamais) |
# MAGIC | `limite_par_utilisateur` | `oui` | cible `moteur` : applique la limite de l'app (`RATE_LIMIT_PER_MINUTE`) à chaque utilisateur simulé |
# MAGIC | `nettoyer` | `oui` | cible `app` : supprime à la fin les conversations créées par le test |
# MAGIC | `url_app` | URL de l'app PROD | adresse de l'app (vide = lue avec `w.apps.get('qualibot')`) |
# MAGIC | `jeton_app` | vide | cible `app` : vide = identité de ce notebook ; sinon un jeton collé (`databricks auth token -p <profil>`, valable 1 h) ou `secret:<scope>/<clé>` |
# MAGIC | `table_messages` | `prod_proj.qualibot.chat_messages` | vraies questions (copie Delta de Lakebase) |
# MAGIC | `table_golden` | `prod_proj.qualibot.eval_golden_retrieval` | questions de référence |
# MAGIC | `table_resultats` | `prod_proj.qualibot.eval_stress_test_runs` | une ligne par question ; vide = rien n'est écrit |
# MAGIC | `graine` | `7` | graine du hasard : même graine et mêmes paramètres = même plan |
# MAGIC
# MAGIC **Limite par utilisateur en cible `app`** : toutes les questions partent sous un seul utilisateur (ce notebook ou le
# MAGIC jeton). Au-delà de `RATE_LIMIT_PER_MINUTE` questions par minute (30 par défaut), l'app les refuse : elles comptent en
# MAGIC `limite`. Le plan affiche combien sont attendues. Pour mesurer la capacité au-delà, relever cette valeur dans l'app
# MAGIC pendant le test, ou utiliser la cible `moteur`.

# COMMAND ----------

dbutils.widgets.dropdown('lancer', 'non', ['non', 'oui'])
dbutils.widgets.dropdown('cible', 'app', ['app', 'moteur'])
dbutils.widgets.dropdown('appels_llm', 'complet', ['complet', 'recherche', 'aucun'])
dbutils.widgets.dropdown('profil', 'realiste', ['vague', 'realiste', 'rejeu'])
dbutils.widgets.text('utilisateurs', '20')
dbutils.widgets.text('duree_s', '60')
dbutils.widgets.dropdown('arrivees', 'poisson', ['poisson', 'reguliere'])
dbutils.widgets.text('tours_par_session', '1-3')
dbutils.widgets.text('reflexion_s', '20')
dbutils.widgets.text('rejeu_jour', 'auto')
dbutils.widgets.text('rejeu_fenetre_min', '60')
dbutils.widgets.text('acceleration', '1')
dbutils.widgets.dropdown('source_questions', 'sessions', ['sessions', 'golden', 'mix'])
dbutils.widgets.dropdown('division', 'reelle', ['reelle', 'ALL', 'AS', 'IS'])
dbutils.widgets.text('timeout_s', '300')
dbutils.widgets.text('max_tours', '200')
dbutils.widgets.text('arret_erreurs_pct', '50')
dbutils.widgets.dropdown('limite_par_utilisateur', 'oui', ['oui', 'non'])
dbutils.widgets.dropdown('nettoyer', 'oui', ['oui', 'non'])
dbutils.widgets.text('url_app', 'https://qualibot-1210945332048776.aws.databricksapps.com')
dbutils.widgets.text('jeton_app', '')
dbutils.widgets.text('table_messages', 'prod_proj.qualibot.chat_messages')
dbutils.widgets.text('table_golden', 'prod_proj.qualibot.eval_golden_retrieval')
dbutils.widgets.text('table_resultats', 'prod_proj.qualibot.eval_stress_test_runs')
dbutils.widgets.text('graine', '7')

import asyncio
import json
import logging
import math
import os
import random
import re
import shlex
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from databricks.sdk import WorkspaceClient

logger = logging.getLogger('stress_test_chat')
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(_handler)
    logger.propagate = False


def _get(name: str) -> str:
    return dbutils.widgets.get(name).strip()


def _range(text: str) -> tuple[int, int]:
    """``'3'`` -> (3, 3), ``'1-3'`` -> (1, 3).

    :param text: one whole number or two joined by a dash
    :return: lowest and highest value, at least 1
    """
    parts = [int(p) for p in re.split(r'\s*-\s*', text) if p.strip()]
    lo, hi = (parts[0], parts[-1]) if parts else (1, 1)
    return max(1, min(lo, hi)), max(1, lo, hi)


P = {
    'app': _get('dossier_app').rstrip('/'),
    'lancer': _get('lancer') == 'oui',
    'cible': _get('cible'),
    'llm': _get('appels_llm'),
    'profil': _get('profil'),
    'utilisateurs': max(1, int(_get('utilisateurs'))),
    'duree_s': max(0.0, float(_get('duree_s'))),
    'arrivees': _get('arrivees'),
    'tours': _range(_get('tours_par_session')),
    'reflexion_s': max(0.0, float(_get('reflexion_s'))),
    'rejeu_jour': _get('rejeu_jour') or 'auto',
    'rejeu_fenetre_min': max(1.0, float(_get('rejeu_fenetre_min'))),
    'acceleration': max(0.01, float(_get('acceleration'))),
    'source': _get('source_questions'),
    'division': _get('division'),
    'timeout_s': max(5.0, float(_get('timeout_s'))),
    'max_tours': max(1, int(_get('max_tours'))),
    'arret_pct': float(_get('arret_erreurs_pct')),
    'limite_utilisateur': _get('limite_par_utilisateur') == 'oui',
    'nettoyer': _get('nettoyer') == 'oui',
    'url_app': _get('url_app').rstrip('/'),
    'table_messages': _get('table_messages'),
    'table_golden': _get('table_golden'),
    'table_resultats': _get('table_resultats'),
    'graine': int(_get('graine')),
}
if P['cible'] == 'app' and P['llm'] != 'complet':
    raise ValueError("La cible app fait toujours le tour complet : appels_llm = complet, ou cible = moteur.")

RUN_ID = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def _app_env(app: str) -> dict[str, str]:
    """The environment the deployed app starts with: ``app.yaml``, then ``target_config.env`` (as ``start.sh``).

    :param app: folder of the deployed app code
    :return: variable name -> value
    """
    env = {}
    with open(f'{app}/app.yaml', encoding='utf-8-sig') as f:
        for item in (yaml.safe_load(f) or {}).get('env') or []:
            if 'value' in item:
                env[item['name']] = str(item['value'])
    target = f'{app}/target_config.env'
    if os.path.exists(target):
        for line in open(target, encoding='utf-8'):
            m = re.match(r'\s*export\s+([A-Z0-9_]+)=(.*)$', line)
            if m:
                env[m.group(1)] = (shlex.split(m.group(2)) or [''])[0]
    return env


APP_ENV = _app_env(P['app'])
RATE_LIMIT = int(APP_ENV.get('RATE_LIMIT_PER_MINUTE', '30') or 30)
if not P['url_app']:
    P['url_app'] = (w.apps.get('qualibot').url or '').rstrip('/')
WS_URL = re.sub(r'^http', 'ws', P['url_app']) + '/api/chat/ws'


def app_token() -> str:
    """The token sent to the app: ``jeton_app`` (raw, or ``secret:<scope>/<key>``), else this notebook's identity."""
    raw = _get('jeton_app')
    if raw.startswith('secret:'):
        scope, key = raw[len('secret:'):].split('/', 1)
        return dbutils.secrets.get(scope, key)
    return raw or w.config.authenticate()['Authorization'].split(' ', 1)[1]


def engine_token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


def run_async(coro) -> Any:
    """``asyncio.run`` in its own thread: the notebook may already run an event loop.

    :param coro: coroutine to run to completion
    :return: its result
    """
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


logger.info('run %s | cible=%s llm=%s profil=%s | app %s | limite app %d questions/min/utilisateur',
            RUN_ID, P['cible'], P['llm'], P['profil'], P['url_app'], RATE_LIMIT)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Questions

# COMMAND ----------

_DIVISION_PREFIX = re.compile(r'^\[Division: (?:AS|IS)\][\s\S]*?\n\n')


def _table_exists(name: str) -> bool:
    try:
        return bool(name) and spark.catalog.tableExists(name)
    except Exception:  # noqa: BLE001 - a missing catalog or schema is a missing table too
        return False


def _tested_sessions() -> set[str]:
    """Session ids created by earlier stress tests, kept out of the real questions."""
    if not _table_exists(P['table_resultats']):
        return set()
    return {r[0] for r in spark.table(P['table_resultats']).select('session_id').distinct().collect() if r[0]}


def load_real_messages() -> pd.DataFrame:
    """The real user questions answered by the app: one row per question, oldest first.

    :return: columns ``session_id``, ``created_at`` (UTC), ``content``, ``division``; empty when the table is missing
    """
    name = P['table_messages']
    if not _table_exists(name):
        logger.warning('table %s absente : pas de vraies conversations', name)
        return pd.DataFrame(columns=['session_id', 'created_at', 'content', 'division'])
    df = spark.table(name)
    cond = "role = 'user' AND length(content) BETWEEN 3 AND 4000"
    if 'deleted' in df.columns:
        cond += ' AND NOT coalesce(deleted, false)'
    if 'status' in df.columns:
        cond += " AND coalesce(status, 'ok') = 'ok'"
    division = 'division' if 'division' in df.columns else "'ALL'"
    pdf = (df.where(cond).selectExpr('session_id', 'created_at', 'content', f'{division} AS division')
           .toPandas())
    pdf = pdf[~pdf['session_id'].isin(_tested_sessions())]
    pdf['content'] = pdf['content'].map(lambda c: _DIVISION_PREFIX.sub('', c))
    pdf['division'] = pdf['division'].fillna('ALL').str.upper()
    pdf['created_at'] = pd.to_datetime(pdf['created_at'], utc=True)
    return pdf.sort_values('created_at').reset_index(drop=True)


def load_golden() -> list[dict[str, str]]:
    """The reference questions, one turn each.

    :return: ``{'content', 'division'}`` dicts; empty when the table is missing
    """
    name = P['table_golden']
    if not _table_exists(name):
        logger.warning('table %s absente : pas de questions de référence', name)
        return []
    df = spark.table(name)
    division = 'division' if 'division' in df.columns else "'ALL'"
    return [{'content': r['question'], 'division': (r['division'] or 'ALL').upper()}
            for r in df.selectExpr('question', f'{division} AS division').collect() if r['question']]


REAL = load_real_messages()
GOLDEN = load_golden()
SESSIONS = [[{'content': r.content, 'division': r.division} for r in g.itertuples()]
            for _, g in REAL.groupby('session_id', sort=False)]
logger.info('%d vraies questions dans %d conversations (%d à plusieurs questions), %d questions de référence',
            len(REAL), len(SESSIONS), sum(len(s) > 1 for s in SESSIONS), len(GOLDEN))
if P['profil'] != 'rejeu' and not SESSIONS and not GOLDEN:
    raise RuntimeError('Aucune question : ni table_messages ni table_golden ne donne de question.')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Plan du test

# COMMAND ----------

RNG = random.Random(P['graine'])


def _division(question: dict[str, str]) -> str:
    return question['division'] if P['division'] == 'reelle' else P['division']


def _conversation(n_turns: int) -> list[dict[str, str]]:
    """The questions of one simulated conversation.

    A real conversation gives its own questions in order (its follow-ups stay follow-ups); a reference question opens a
    conversation and other reference questions follow it.

    :param n_turns: questions wanted
    :return: ``{'content', 'division'}`` dicts, ``n_turns`` at most for a real conversation shorter than that
    """
    use_real = SESSIONS and (P['source'] == 'sessions' or not GOLDEN or (P['source'] == 'mix' and RNG.random() < .5))
    if use_real:
        longer = [s for s in SESSIONS if len(s) >= n_turns]
        session = RNG.choice(longer or SESSIONS)
        turns = session[:n_turns]
    else:
        turns = [RNG.choice(GOLDEN) for _ in range(n_turns)]
    return [{'content': t['content'], 'division': _division(t)} for t in turns]


def _think() -> float:
    return P['reflexion_s'] * RNG.uniform(.5, 1.5)


def _replay_window() -> tuple[pd.Timestamp, pd.Timestamp]:
    """The real period replayed: the given start, or the busiest one of ``rejeu_fenetre_min`` minutes."""
    width = pd.Timedelta(minutes=P['rejeu_fenetre_min'])
    if P['rejeu_jour'] != 'auto':
        start = pd.Timestamp(P['rejeu_jour'], tz='UTC')
        return start, start + width
    times = REAL['created_at'].dt.tz_convert(None).to_numpy()
    counts = np.searchsorted(times, times + width.to_timedelta64()) - np.arange(len(times))
    start = REAL['created_at'].iloc[int(np.argmax(counts))]
    return start, start + width


def build_plan() -> list[dict[str, Any]]:
    """One entry per simulated user: when it starts and its questions, each with its wait before it is sent.

    ``wait_s`` of a question: ``vague`` and ``realiste``, the reading time after the previous answer; ``rejeu``, its
    real time from the start of the period, divided by ``acceleration`` (``at_s``).

    :return: users ``{'user', 'start_s', 'turns': [{'content', 'division', 'wait_s' | 'at_s'}]}``
    """
    users = []
    if P['profil'] == 'rejeu':
        if REAL.empty:
            raise RuntimeError(f"Le profil rejeu lit {P['table_messages']}, absente ou vide.")
        start, end = _replay_window()
        window = REAL[(REAL['created_at'] >= start) & (REAL['created_at'] < end)]
        logger.info('période rejouée : %s -> %s UTC, %d questions', start, end, len(window))
        for i, (_, g) in enumerate(window.groupby('session_id', sort=False)):
            at = [(t - start).total_seconds() / P['acceleration'] for t in g['created_at']]
            users.append({'user': f'u{i:03d}', 'start_s': at[0],
                          'turns': [{'content': r.content, 'division': _division({'division': r.division}),
                                     'at_s': a} for r, a in zip(g.itertuples(), at)]})
        return sorted(users, key=lambda u: u['start_s'])
    n = P['utilisateurs']
    if P['profil'] == 'vague':
        starts = [0.0] * n
    elif P['arrivees'] == 'reguliere':
        starts = [i * P['duree_s'] / n for i in range(n)]
    else:   # Poisson arrivals: independent exponential gaps, scaled to end inside the window
        gaps = [RNG.expovariate(1.0) for _ in range(n)]
        total = sum(gaps) or 1.0
        starts, acc = [], 0.0
        for g in gaps:
            starts.append(acc / total * P['duree_s'])
            acc += g
    for i, s in enumerate(starts):
        k = RNG.randint(*P['tours'])
        turns = _conversation(k)
        for j, t in enumerate(turns):
            t['wait_s'] = 0.0 if j == 0 else _think()
        users.append({'user': f'u{i:03d}', 'start_s': s, 'turns': turns})
    return users


def plan_summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
    """What the plan sends, assuming each answer takes ``typical_s`` (for the per-minute peak only).

    :param plan: result of ``build_plan``
    :return: counts and the peak of questions sent in one minute
    """
    typical_s = 25.0
    sends = []
    for u in plan:
        t = u['start_s']
        for j, turn in enumerate(u['turns']):
            if 'at_s' in turn:
                t = max(turn['at_s'], t)
            elif j:
                t += typical_s + turn['wait_s']
            sends.append(t)
            t += typical_s if 'at_s' in turn else 0
    sends = sorted(sends)[:P['max_tours']]
    peak = max((sum(1 for x in sends if s <= x < s + 60) for s in sends), default=0)
    return {'utilisateurs': len(plan), 'questions': len(sends), 'plafond_max_tours': P['max_tours'],
            'questions_multi_tours': sum(len(u['turns']) > 1 for u in plan),
            'pic_questions_par_minute': peak, 'duree_estimee_min': round((sends[-1] + typical_s) / 60, 1) if sends else 0}


PLAN = build_plan()
SUMMARY = plan_summary(PLAN)
display(pd.DataFrame([SUMMARY]))
expected_refused = max(0, SUMMARY['pic_questions_par_minute'] - RATE_LIMIT)
if P['cible'] == 'app' and expected_refused:
    logger.warning('cible app : un seul utilisateur, %d questions/min au pic pour une limite de %d. Environ %d '
                   'questions seront refusées par l\'app (statut limite).', SUMMARY['pic_questions_par_minute'],
                   RATE_LIMIT, expected_refused)
logger.info('exemple de conversation : %s', [t['content'][:80] for t in PLAN[0]['turns']] if PLAN else '-')
if not P['lancer']:
    logger.info('lancer = non : plan seul, rien n\'a été envoyé. Passer lancer = oui pour lancer le test.')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Une question, comme l'app

# COMMAND ----------

import websockets

TOO_MANY = 'Too many requests'


async def ask_app(messages: list[dict[str, str]], division: str, session_id: str, token: str) -> dict[str, Any]:
    """One turn through the deployed app, as the browser sends it.

    :param messages: conversation so far, ending with the question
    :param division: ``ALL``, ``AS`` or ``IS``
    :param session_id: conversation id (a UUID, as the browser makes it)
    :param token: bearer token of the caller
    :return: ``status`` (``ok``, ``erreur``, ``limite``, ``refus``), ``ttft_s``, ``answer`` and ``n_sources`` when ok,
        ``error`` otherwise
    """
    t0 = time.monotonic()
    first = None
    headers = {'Authorization': f'Bearer {token}', 'Origin': P['url_app']}
    async with websockets.connect(WS_URL, additional_headers=headers, max_size=None, open_timeout=60,
                                  ping_interval=20) as ws:
        await ws.send(json.dumps({'messages': messages, 'session_id': session_id, 'division': division}))
        while True:
            try:
                m = json.loads(await ws.recv())
            except websockets.ConnectionClosed as exc:
                return {'status': 'refus', 'error': f'connexion fermée par l\'app sans réponse ({exc.code})'}
            kind = m.get('type')
            if kind == 'delta' and first is None:
                first = time.monotonic() - t0
            elif kind == 'done':
                return {'status': 'ok', 'ttft_s': first if first is not None else time.monotonic() - t0,
                        'answer': m.get('content') or '', 'n_sources': len(m.get('sources') or [])}
            elif kind == 'error':
                err = m.get('error') or ''
                return {'status': 'limite' if err.startswith(TOO_MANY) else 'erreur', 'ttft_s': first, 'error': err}


ENGINE: dict[str, Any] = {}


def load_engine() -> None:
    """The deployed app's code and environment in this notebook, as ``start.sh`` starts it."""
    os.environ.update(APP_ENV)
    os.environ['ENV'] = 'production'
    if P['app'] not in sys.path:
        sys.path.insert(0, P['app'])
    for mod in [m for m in sys.modules if m.split('.')[0] in ('server', 'utils')]:
        del sys.modules[mod]
    from server.routers.chat.helpers import (
        _apply_citation_markers, _clean_messages, _number_sources, _trim_history, _with_today_date,
    )
    from server.services.chat import translation_bridge
    from server.services.chat.config import TRANSLATE_BRIDGE_ENABLED
    from server.services.chat.engine import normalize_division, stream_chat
    from server.services.chat.engine import retrieval
    from server.services.chat.turn_log import TurnLog
    from server.services.infra.doc_catalog import augment_sources, with_document_info
    from server.services.infra.http_clients import close_shared_clients
    from server.services.infra.user import check_rate_limit
    ENGINE.update(locals())
    if P['llm'] == 'aucun':
        async def _no_llm(*_args, **_kwargs):
            raise RuntimeError('stress test without LLM calls')
        # The app's own path when the rewrite fails: the question is searched as asked.
        retrieval.translate_query = _no_llm
        retrieval.rewrite_queries = _no_llm


def _turn_facts(log) -> dict[str, Any]:
    """Step durations, model facts and warnings of a turn log."""
    d = log.data
    return {'timings_ms': dict(log.timings_ms), 'issues': log.codes, 'answer_endpoint': d.get('answer_endpoint'),
            'input_tokens': d.get('input_tokens'), 'output_tokens': d.get('output_tokens'),
            'cost_eur': d.get('cost_eur'), 'queue_wait_ms': log.timings_ms.get('queue_wait'),
            'error_detail': (log.error or {}).get('message')}


async def ask_engine(messages: list[dict[str, str]], division: str, user_key: str, token: str) -> dict[str, Any]:
    """One turn through the app's engine, step by step as ``server/routers/chat/ws.py`` runs it (without the save).

    :param messages: conversation so far, ending with the question
    :param division: ``ALL``, ``AS`` or ``IS``
    :param user_key: simulated user, for the app's per-user limit
    :param token: Databricks token for the model and Vector Search calls
    :return: same shape as ``ask_app``, plus the turn log facts
    """
    e = ENGINE
    if P['limite_utilisateur'] and e['check_rate_limit'](user_key):
        return {'status': 'limite', 'error': 'limite par utilisateur de l\'app (RATE_LIMIT_PER_MINUTE)'}
    log = e['TurnLog']()
    t0 = time.monotonic()
    msgs = e['_trim_history'](e['_clean_messages'](messages))
    user_content = next((m['content'] for m in reversed(msgs) if m['role'] == 'user'), '')
    if P['llm'] != 'complet':
        conversation = e['retrieval']._clean_history(e['_with_today_date'](msgs))
        try:
            found = await e['retrieval'].retrieve_for_turn(host=HOST, token=token,
                                                           division=e['normalize_division'](division),
                                                           conversation=conversation, log=log)
        except Exception as exc:  # noqa: BLE001 - a failed search is a result
            log.fail('search', type(exc).__name__, str(exc)[:300], exc=exc)
            log.finish('error')
            return {'status': 'erreur', 'error': f'{type(exc).__name__}: {str(exc)[:300]}', **_turn_facts(log)}
        status = log.finish('ok')
        return {'status': 'ok' if status == 'ok' else 'degrade', 'ttft_s': time.monotonic() - t0, 'answer': '',
                'n_sources': len({r.get('REF') for r in found['rows']}), **_turn_facts(log)}

    tb = e['translation_bridge']
    ctx, messages_for_engine = None, msgs
    if e['TRANSLATE_BRIDGE_ENABLED'] and user_content:
        with log.timed('translate_in'):
            en_question, ctx = await tb.translate_question_to_en(
                user_content, HOST, token, record=log.model_io.setdefault('translate_in', {}))
        if ctx.error:
            log.issue('translate_in_failed', 'translate_in', ctx.error)
        if ctx.needs_translation:
            messages_for_engine = [dict(m) for m in msgs]
            for i in range(len(messages_for_engine) - 1, -1, -1):
                if messages_for_engine[i]['role'] == 'user':
                    messages_for_engine[i]['content'] = en_question
                    break
    language = tb.answer_language(ctx, user_content) if user_content else None
    stream = e['stream_chat'](HOST, token, division, e['_with_today_date'](messages_for_engine),
                              answer_language=language, log=log)
    accumulated, sources, citations, first = [], [], [], None
    outcome, result = 'error', None
    try:
        async for chunk in stream:
            if chunk.startswith('data: [DONE]'):
                if not (user_content and accumulated):
                    log.fail('llm', 'EmptyAnswer', 'stream ended with no text')
                    result = {'status': 'erreur', 'error': 'réponse vide'}
                    break
                final = e['_apply_citation_markers'](''.join(accumulated), citations)
                sources = await asyncio.to_thread(e['augment_sources'], ''.join(accumulated), sources)
                e['_number_sources'](sources, citations)
                e['with_document_info'](sources)
                if ctx:
                    with log.timed('translate_back'):
                        final = await tb.translate_answer_back(final, ctx, HOST, token,
                                                               record=log.model_io.setdefault('translate_back', {}))
                    if ctx.answer_error:
                        log.issue('translate_back_failed', 'translate_back', ctx.answer_error)
                outcome = 'ok'
                result = {'status': 'ok', 'answer': final, 'n_sources': len(sources),
                          'ttft_s': first if first is not None else time.monotonic() - t0}
                break
            if not chunk.startswith('data: '):
                continue
            try:
                ev = json.loads(chunk[6:].strip())
            except json.JSONDecodeError:
                continue
            kind = ev.get('type', '')
            if kind == 'response.output_text.delta':
                accumulated.append(ev.get('delta', ''))
                # The browser sees no text before the translated answer when the question was translated.
                if first is None and not (ctx and ctx.needs_translation):
                    first = time.monotonic() - t0
            elif kind == 'sources':
                sources = ev.get('sources') if isinstance(ev.get('sources'), list) else []
                citations = ev.get('citations') if isinstance(ev.get('citations'), list) else []
            elif kind == 'error':
                if not log.error:
                    log.fail('engine', ev.get('error_type', 'ChatLLMError'), ev.get('error', ''))
                result = {'status': 'erreur', 'ttft_s': first, 'error': ev.get('error', '')}
                break
        if result is None:
            result = {'status': 'erreur', 'error': 'flux terminé sans fin de réponse'}
    finally:
        await stream.aclose()
    status = log.finish(outcome)
    if result['status'] == 'ok' and status == 'degraded':
        result['status'] = 'degrade'
    return {**result, **_turn_facts(log)}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Lancement

# COMMAND ----------

_SCHEMA = """run_id string, run_started_at timestamp, cible string, appels_llm string, profil string, parametres string,
user string, session_id string, tour int, division string, question string, depart_s double, en_vol_au_depart int,
ttft_s double, total_s double, statut string, erreur string, reponse_chars int, n_sources int, answer_endpoint string,
queue_wait_ms int, input_tokens int, output_tokens int, cost_eur double, etapes_ms string, avertissements string"""


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


async def run_test(plan: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Run the plan: one task per simulated user, each turn sent at its planned time.

    :param plan: result of ``build_plan``
    :return: one row per question sent, and why the run stopped early ('' when it did not)
    """
    rows: list[dict[str, Any]] = []
    state = {'in_flight': 0, 'sent': 0, 'stop': ''}
    t_run = time.monotonic()
    run_started = datetime.now(timezone.utc)

    def stopping() -> bool:
        if state['stop']:
            return True
        if state['sent'] >= P['max_tours']:
            state['stop'] = f"plafond max_tours ({P['max_tours']}) atteint"
            return True
        last = rows[-20:]
        if P['arret_pct'] > 0 and len(last) >= 10:
            failed = sum(r['statut'] not in ('ok', 'degrade') for r in last)
            if failed * 100 / len(last) > P['arret_pct']:
                state['stop'] = f"{failed} échecs sur les {len(last)} dernières questions (> {P['arret_pct']:.0f} %)"
                return True
        return False

    async def one(messages, division, user, session_id) -> dict[str, Any]:
        try:
            if P['cible'] == 'app':
                coro = ask_app(messages, division, session_id, app_token())
            else:
                coro = ask_engine(messages, division, f'stress-{RUN_ID}-{user}', engine_token())
            return await asyncio.wait_for(coro, P['timeout_s'])
        except asyncio.TimeoutError:
            return {'status': 'timeout', 'error': f"pas de fin après {P['timeout_s']:.0f} s"}
        except Exception as exc:  # noqa: BLE001 - a refused connection is a result too
            return {'status': 'refus', 'error': f'{type(exc).__name__}: {str(exc)[:300]}'}

    async def user_task(u) -> None:
        session_id, history = str(uuid.uuid4()), []
        await asyncio.sleep(max(0.0, u['start_s'] - (time.monotonic() - t_run)))
        for j, turn in enumerate(u['turns']):
            if 'at_s' in turn:
                await asyncio.sleep(max(0.0, turn['at_s'] - (time.monotonic() - t_run)))
            elif j:
                await asyncio.sleep(turn['wait_s'])
            if stopping():
                return
            state['sent'] += 1
            messages = history + [{'role': 'user', 'content': turn['content']}]
            start = time.monotonic() - t_run
            in_flight = state['in_flight']
            state['in_flight'] += 1
            try:
                out = await one(messages, turn['division'], u['user'], session_id)
            finally:
                state['in_flight'] -= 1
            total = time.monotonic() - t_run - start
            rows.append({
                'run_id': RUN_ID, 'run_started_at': run_started, 'cible': P['cible'], 'appels_llm': P['llm'],
                'profil': P['profil'], 'parametres': json.dumps({k: v for k, v in P.items() if k != 'lancer'},
                                                                 default=str),
                'user': u['user'], 'session_id': session_id, 'tour': j + 1, 'division': turn['division'],
                'question': turn['content'][:1000], 'depart_s': round(start, 3), 'en_vol_au_depart': in_flight,
                'ttft_s': _num(out.get('ttft_s')), 'total_s': round(total, 3), 'statut': out['status'],
                'erreur': (out.get('error') or out.get('error_detail') or None),
                'reponse_chars': len(out.get('answer') or '') or None, 'n_sources': out.get('n_sources'),
                'answer_endpoint': out.get('answer_endpoint'), 'queue_wait_ms': out.get('queue_wait_ms'),
                'input_tokens': out.get('input_tokens'), 'output_tokens': out.get('output_tokens'),
                'cost_eur': _num(out.get('cost_eur')), 'etapes_ms': json.dumps(out.get('timings_ms') or {}),
                'avertissements': ','.join(out.get('issues') or []) or None})
            done = len(rows)
            if done % 10 == 0:
                logger.info('%d questions terminées, %d en cours', done, state['in_flight'])
            if out['status'] in ('ok', 'degrade'):
                history = messages + [{'role': 'assistant', 'content': out.get('answer') or ''}]
            # A failed turn is left out of the history, as the browser does (history.ts).

    try:
        await asyncio.gather(*(user_task(u) for u in plan))
    finally:
        if P['cible'] == 'moteur':
            await ENGINE['close_shared_clients']()
            await ENGINE['translation_bridge'].shutdown_http_client()
    return rows, state['stop']


ROWS: list[dict[str, Any]] = []
STOP_REASON = ''
PROBE_SESSION = str(uuid.uuid4())
if P['lancer']:
    if P['cible'] == 'app':
        probe = run_async(ask_app([{'role': 'user', 'content': 'Bonjour'}], 'ALL', PROBE_SESSION, app_token()))
        if probe['status'] != 'ok':
            raise RuntimeError(f"L'app refuse ce notebook ({probe.get('error')}). Coller un jeton dans jeton_app "
                               "(databricks auth token -p <profil PROD>), ou cible = moteur.")
        logger.info('sonde ok : l\'app répond à ce notebook en %.1f s', probe.get('ttft_s') or 0)
    else:
        load_engine()
    logger.info('départ : %d questions prévues', SUMMARY['questions'])
    ROWS, STOP_REASON = run_async(run_test(PLAN))
    logger.info('fin : %d questions%s', len(ROWS), f' ; arrêt anticipé : {STOP_REASON}' if STOP_REASON else '')
    if P['table_resultats'] and ROWS:
        (spark.createDataFrame(ROWS, schema=_SCHEMA)
         .write.mode('append').option('mergeSchema', 'true').saveAsTable(P['table_resultats']))
        logger.info('résultats écrits dans %s (run_id = %s)', P['table_resultats'], RUN_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Résultats

# COMMAND ----------

STATUS_COLORS = {'ok': '#2E7D5B', 'degrade': '#D99A1E', 'erreur': '#C8453B', 'limite': '#7E57C2',
                 'timeout': '#5B6770', 'refus': '#8D6E63'}
STATUS_LABELS = {'ok': 'réponse', 'degrade': 'réponse dégradée', 'erreur': 'erreur affichée',
                 'limite': 'refus (limite par utilisateur)', 'timeout': 'sans fin (timeout)',
                 'refus': 'connexion refusée'}
plt.rcParams.update({'figure.dpi': 110, 'axes.spines.top': False, 'axes.spines.right': False,
                     'axes.grid': True, 'grid.alpha': .25, 'axes.titleweight': 'bold', 'axes.titlesize': 12,
                     'font.size': 10, 'legend.frameon': False})


def _pct(s: pd.Series, q: float) -> float | None:
    s = s.dropna()
    return round(float(s.quantile(q)), 1) if len(s) else None


def kpis(df: pd.DataFrame) -> pd.DataFrame:
    """The key figures of a run, in one row."""
    answered = df[df['statut'].isin(['ok', 'degrade'])]
    span = max(1e-9, float((df['depart_s'] + df['total_s']).max() - df['depart_s'].min()))
    return pd.DataFrame([{
        'questions': len(df), 'réponses %': round(len(answered) * 100 / max(1, len(df)), 1),
        'dégradées': int((df['statut'] == 'degrade').sum()), 'erreurs': int((df['statut'] == 'erreur').sum()),
        'refus limite': int((df['statut'] == 'limite').sum()), 'timeouts': int((df['statut'] == 'timeout').sum()),
        'connexions refusées': int((df['statut'] == 'refus').sum()),
        '1er mot p50 s': _pct(answered['ttft_s'], .5), '1er mot p95 s': _pct(answered['ttft_s'], .95),
        'total p50 s': _pct(answered['total_s'], .5), 'total p95 s': _pct(answered['total_s'], .95),
        'total max s': _pct(answered['total_s'], 1.0),
        'réponses / min': round(len(answered) / span * 60, 1), 'pic en cours': int(in_flight_curve(df)[1].max()),
        'attente file p95 s': _pct(df['queue_wait_ms'].astype(float) / 1000, .95),
        'coût €': round(float(df['cost_eur'].fillna(0).sum()), 3) if df['cost_eur'].notna().any() else None,
    }])


def in_flight_curve(df: pd.DataFrame, step: float = .25) -> tuple[np.ndarray, np.ndarray]:
    """Questions in progress over time, sampled every ``step`` seconds."""
    end = float((df['depart_s'] + df['total_s']).max()) if len(df) else 0.0
    t = np.arange(0, end + step, step)
    starts, ends = df['depart_s'].to_numpy(), (df['depart_s'] + df['total_s']).to_numpy()
    return t, ((starts[None, :] <= t[:, None]) & (ends[None, :] > t[:, None])).sum(axis=1)


def _legend_handles(statuses) -> list:
    from matplotlib.patches import Patch
    return [Patch(color=STATUS_COLORS[s], label=STATUS_LABELS[s]) for s in STATUS_COLORS if s in set(statuses)]


def chart_timeline(df: pd.DataFrame) -> None:
    """One line per question: light while waiting for the first word, dark while the answer is written."""
    d = df.sort_values('depart_s').reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(13, max(3.5, min(12, .06 * len(d) + 2.5))))
    for i, r in d.iterrows():
        color = STATUS_COLORS.get(r['statut'], '#999')
        first = r['ttft_s'] if pd.notna(r['ttft_s']) else 0.0
        ax.barh(i, first, left=r['depart_s'], height=.8, color=color, alpha=.35, linewidth=0)
        ax.barh(i, max(0, r['total_s'] - first), left=r['depart_s'] + first, height=.8, color=color, linewidth=0)
    ax.invert_yaxis()
    ax.set_xlabel('secondes depuis le départ du test')
    ax.set_ylabel('questions, dans l\'ordre d\'envoi')
    ax.set_title('Chronologie : clair = attente du premier mot, foncé = écriture de la réponse', pad=28)
    ax.legend(handles=_legend_handles(d['statut']), loc='lower center', bbox_to_anchor=(.5, 1.0), ncol=6)
    ax.grid(axis='y', visible=False)
    plt.tight_layout()
    plt.show()


def chart_load(df: pd.DataFrame) -> None:
    """Top: questions in progress and questions sent per 5 s. Bottom: time to the first word and total time of each
    answer, at its end, with the p95 over the last 15 answers."""
    t, live = in_flight_curve(df)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 7.5), sharex=True, gridspec_kw={'height_ratios': [1, 1.3]})
    width = max(1.0, float(np.ceil(t.max() / 60)))
    bins = np.arange(0, t.max() + width, width)
    a1.hist(df['depart_s'], bins=bins, color='#90A4AE', alpha=.55, label=f'questions envoyées (par {width:.0f} s)')
    a1.step(t, live, where='post', color='#1F4E79', linewidth=2, label='questions en cours')
    refused = df[df['statut'] == 'limite']
    if len(refused):
        a1.scatter(refused['depart_s'], np.zeros(len(refused)), marker='v', color=STATUS_COLORS['limite'],
                   label='refus limite', zorder=3)
    a1.set_ylabel('questions')
    a1.set_title('Charge')
    a1.legend(loc='upper right')
    d = df.assign(fin_s=df['depart_s'] + df['total_s']).sort_values('fin_s')
    ok = d[d['statut'].isin(['ok', 'degrade'])]
    for s, g in d.groupby('statut'):
        a2.scatter(g['fin_s'], g['total_s'], s=22, color=STATUS_COLORS.get(s, '#999'), alpha=.85,
                   label=f'temps total : {STATUS_LABELS.get(s, s)}')
    if len(ok):
        a2.scatter(ok['fin_s'], ok['ttft_s'], s=14, marker='x', color='#1F4E79', alpha=.7, label='premier mot')
        roll = ok['total_s'].rolling(15, min_periods=3).quantile(.95)
        a2.plot(ok['fin_s'], roll, color='#C8453B', linewidth=1.8, label='p95 du temps total (15 dernières)')
    a2.set_xlabel('secondes depuis le départ du test')
    a2.set_ylabel('secondes')
    a2.set_title('Latence au fil du test')
    a2.legend(loc='upper center', bbox_to_anchor=(.5, -.16), fontsize=8, ncol=4)
    plt.tight_layout()
    plt.show()


def chart_distribution(df: pd.DataFrame) -> None:
    """Share of the answers obtained within x seconds, for the first word and for the whole answer."""
    ok = df[df['statut'].isin(['ok', 'degrade'])]
    if ok.empty:
        logger.info('aucune réponse : pas de distribution')
        return
    fig, ax = plt.subplots(figsize=(13, 4.5))
    for col, label, color in [('ttft_s', 'premier mot', '#1F4E79'), ('total_s', 'réponse complète', '#2E7D5B')]:
        v = np.sort(ok[col].dropna().to_numpy())
        if not len(v):
            continue
        ax.plot(v, np.arange(1, len(v) + 1) / len(v) * 100, color=color, linewidth=2.2, label=label)
        for q, ls in [(.5, '--'), (.95, ':')]:
            x = float(np.quantile(v, q))
            ax.axvline(x, color=color, linestyle=ls, linewidth=1)
            ax.annotate(f'p{int(q * 100)} {x:.1f} s', (x, q * 100), xytext=(4, -12), textcoords='offset points',
                        color=color, fontsize=9)
    ax.set_xlabel('secondes')
    ax.set_ylabel('% des réponses obtenues')
    ax.set_ylim(0, 102)
    ax.set_title('Distribution des temps (courbe cumulée)')
    ax.legend(loc='lower right')
    plt.tight_layout()
    plt.show()


STAGES = [('translate_in', 'traduction question'), ('rewrite', 'réécriture / traduction'),
          ('search', 'recherche (Vector Search, REF)'), ('queue_wait', 'attente d\'un créneau de réponse'),
          ('generation', 'génération de la réponse'), ('translate_back', 'traduction réponse')]
STAGE_COLORS = ['#B0BEC5', '#90CAF9', '#1F4E79', '#C8453B', '#2E7D5B', '#80CBC4']


def chart_stages(df: pd.DataFrame) -> None:
    """Cible ``moteur``: median and p95 of each step of a turn, and the wait for an answer slot against the load."""
    t = pd.DataFrame([json.loads(x or '{}') for x in df['etapes_ms']], index=df.index)
    if t.empty or 'retrieval' not in t:
        logger.info('pas de détail des étapes (cible app : voir chat_turns dans Lakebase)')
        return
    t['search'] = t['retrieval'] - (t['rewrite'].fillna(0) if 'rewrite' in t else 0)
    present = [(k, lbl, c) for (k, lbl), c in zip(STAGES, STAGE_COLORS) if k in t and t[k].notna().any()]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.5), gridspec_kw={'width_ratios': [1.4, 1]})
    for row, q in enumerate([.5, .95]):
        left = 0.0
        for k, lbl, c in present:
            v = float(t[k].fillna(0).quantile(q)) / 1000
            a1.barh(row, v, left=left, color=c, label=lbl if row == 0 else None)
            if v > .8:
                a1.text(left + v / 2, row, f'{v:.1f}', ha='center', va='center', color='white', fontsize=8)
            left += v
    a1.set_yticks([0, 1], ['médiane', 'p95'])
    a1.invert_yaxis()
    a1.set_xlabel('secondes (chaque étape prise à son propre percentile)')
    a1.set_title('Où passe le temps d\'un tour')
    a1.legend(loc='upper center', bbox_to_anchor=(.5, -.22), ncol=3, fontsize=8)
    if 'queue_wait' in t and t['queue_wait'].notna().any():
        a2.scatter(df['en_vol_au_depart'], t['queue_wait'].fillna(0) / 1000, s=18, color='#C8453B', alpha=.7)
        a2.set_xlabel('questions déjà en cours à l\'envoi')
        a2.set_ylabel('attente d\'un créneau (s)')
        a2.set_title('File d\'attente des réponses')
    else:
        a2.axis('off')
    plt.tight_layout()
    plt.show()


if ROWS:
    DF = pd.DataFrame(ROWS)
    display(kpis(DF))
    chart_timeline(DF)
    chart_load(DF)
    chart_distribution(DF)
    if P['cible'] == 'moteur':
        chart_stages(DF)
    failures = DF[~DF['statut'].isin(['ok', 'degrade'])]
    if len(failures):
        display(failures.assign(erreur=failures['erreur'].fillna('').str.slice(0, 200))
                .groupby(['statut', 'erreur']).size().rename('questions').reset_index()
                .sort_values('questions', ascending=False))
    if DF['avertissements'].notna().any():
        display(DF['avertissements'].dropna().str.split(',').explode().value_counts()
                .rename('questions').rename_axis('avertissement').reset_index())
    display(DF[['user', 'tour', 'division', 'question', 'depart_s', 'en_vol_au_depart', 'ttft_s', 'total_s',
                'statut', 'erreur', 'answer_endpoint']].sort_values('depart_s'))
    if STOP_REASON:
        logger.warning('arrêt anticipé : %s', STOP_REASON)
else:
    logger.info('aucun résultat (lancer = non)')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Nettoyage (cible `app`) et historique des runs

# COMMAND ----------


def delete_sessions(session_ids: list[str]) -> tuple[int, int]:
    """Soft-delete the test conversations through the app, as its delete button does.

    :param session_ids: conversations created by this run
    :return: deleted, failed
    """
    headers = {'Authorization': f'Bearer {app_token()}', 'Origin': P['url_app']}
    ok = failed = 0
    with httpx.Client(timeout=30) as client:
        for sid in session_ids:
            r = client.delete(f"{P['url_app']}/api/chat/sessions/{sid}", headers=headers)
            ok, failed = (ok + 1, failed) if r.status_code == 200 else (ok, failed + 1)
    return ok, failed


if ROWS and P['cible'] == 'app' and P['nettoyer']:
    sessions = sorted({r['session_id'] for r in ROWS if r['statut'] in ('ok', 'degrade', 'erreur', 'timeout')}
                      | {PROBE_SESSION})
    done, failed = delete_sessions(sessions)
    logger.info('nettoyage : %d conversations supprimées, %d non trouvées ou en échec', done, failed)

if P['table_resultats'] and _table_exists(P['table_resultats']):
    display(spark.sql(f"""
        SELECT run_id, min(run_started_at) AS debut, first(cible) AS cible, first(appels_llm) AS appels_llm,
               first(profil) AS profil, count(*) AS questions,
               round(avg(CASE WHEN statut IN ('ok', 'degrade') THEN 1 ELSE 0 END) * 100, 1) AS reponses_pct,
               count_if(statut = 'limite') AS refus_limite, max(en_vol_au_depart) + 1 AS pic_en_cours,
               round(percentile(ttft_s, 0.5), 1) AS premier_mot_p50_s, round(percentile(ttft_s, 0.95), 1) AS premier_mot_p95_s,
               round(percentile(total_s, 0.5), 1) AS total_p50_s, round(percentile(total_s, 0.95), 1) AS total_p95_s
        FROM {P['table_resultats']} GROUP BY run_id ORDER BY run_id DESC LIMIT 20"""))
