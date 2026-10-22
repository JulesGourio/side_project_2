"""The comparison's prompts: files under ``server/prompts/compare/``, never strings in Python."""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / 'prompts' / 'compare'


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """The text of a comparison prompt, read once and returned exactly as stored in its file.

    :param name: file name without extension, e.g. ``summary`` for ``summary.md``
    :return: the prompt text
    """
    with open(PROMPTS_DIR / f'{name}.md', encoding='utf-8', newline='') as f:
        return f.read()
