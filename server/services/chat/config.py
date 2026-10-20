"""Settings of the chat service outside the engine: translation bridge, language detection, MLflow export.

The engine's own settings are in ``engine/config.py``. Values read from the environment are read once, at import.
"""

import os
import re
from pathlib import Path


# --- Translation bridge (translation_bridge.py) ---

TRANSLATE_BRIDGE_ENABLED = os.getenv('CHAT_TRANSLATE_BRIDGE_ENABLED', 'false').lower() == 'true'
TRANSLATE_ENDPOINT = os.getenv('CHAT_TRANSLATE_ENDPOINT', 'databricks-gpt-6-luna')
# Tried in order when TRANSLATE_ENDPOINT fails (429 included): the chatbot runs on GPT-6 Luna + GPT-5.6 Luna, each the
# other's backup. Empty value = no fallback.
TRANSLATE_FALLBACK_ENDPOINTS = [e.strip() for e in os.getenv('CHAT_TRANSLATE_FALLBACK_ENDPOINTS',
                                                             'databricks-gpt-5-6-luna').split(',') if e.strip()]
TRANSLATE_TIMEOUT_S = 20.0

# fastText's lid.176 model: langdetect is noisy on short or technical text.
FASTTEXT_MODEL_PATH = str(Path(__file__).resolve().parents[3] / 'third_party' / 'fasttext' / 'lid.176.ftz')

# Below this confidence a fastText guess is noise: the LLM decides.
FASTTEXT_CONFIDENCE_THRESHOLD = 0.65

# Skip the LLM round trip entirely for the two languages the corpus and the
# answer prompt's language rule already handle natively.
NO_TRANSLATION_NEEDED = {'fr', 'en'}

# An answer is only declared "in the wrong language" above this confidence: a false alarm
# sends a correct answer through a translation it does not need.
MISMATCH_CONFIDENCE_THRESHOLD = 0.8

# English names of the languages met in Latécoère's sites (FR, CZ, BG, MX, BR) and a few
# neighbours — named in the answer-language reminder (chat/engine/answer.py).
LANGUAGE_NAMES = {'fr': 'French', 'en': 'English', 'es': 'Spanish', 'cs': 'Czech', 'bg': 'Bulgarian',
                  'pt': 'Portuguese', 'de': 'German', 'it': 'Italian', 'pl': 'Polish', 'ro': 'Romanian',
                  'sk': 'Slovak', 'nl': 'Dutch', 'ru': 'Russian', 'uk': 'Ukrainian'}

# What says nothing about the language and misleads fastText: document codes (REF-like tokens), anything with a digit,
# URLs, e-mails, citation markers, code.
CODE_BLOCK_RE = re.compile(r'```.*?```', re.S)
NOISE_RE = re.compile(r'https?://\S+|\S+@\S+|⟦\d+⟧|\[\d+\]|`[^`]*`|\b\S*\d\S*\b|\b[A-Z]{2,}(?:[-_/][A-Z0-9]+)+\b')
# In an answer: quoted passages (the instructions quote documents in their own language),
# blockquotes and table rows (document titles, values) are not the answer's language.
QUOTED_RE = re.compile(r'«[^»]*»|“[^”]*”|"[^"\n]{12,}"')
LETTERS_RE = re.compile(r'[^\W\d_]{2,}')


# --- MLflow export (mlflow_turns.py) ---

MLFLOW_MAX_DOC_CHARS = 20_000          # passage text kept per document in the retriever span output
MLFLOW_MAX_ISSUE_CHARS = 500
MLFLOW_RETRY_MISSING_S = 60            # wait before looking again for an experiment that was not found
MLFLOW_PROMPT_TAG = 'chat_prompt.'     # experiment tag prefix written by register_chat_prompts
MLFLOW_PROMPT_EMPTY_TTL_S = 30         # nothing registered yet: look again soon
MLFLOW_PROMPT_TTL_S = 600
MLFLOW_EXPORT_TIMEOUT_S = 10
