"""The comparison prompts are files under ``server/prompts/compare/`` loaded as stored, with no placeholder to fill."""

import re
from pathlib import Path

import pytest

from server.services.compare.prompts import PROMPTS_DIR, load_prompt

NAMES = ['analysis_default', 'analysis_standard', 'analysis_structured', 'summary', 'summary_image', 'impact_judge']
_REPO = Path(__file__).resolve().parents[1]


def test_every_file_is_listed_and_loads() -> None:
    """Each prompt file is in NAMES, loads and is not empty."""
    assert sorted(p.stem for p in PROMPTS_DIR.glob('*.md')) == sorted(NAMES)
    for name in NAMES:
        assert load_prompt(name).strip()


@pytest.mark.parametrize('name', NAMES)
def test_text_is_not_altered_by_loading(name: str) -> None:
    """The loaded text is the file's bytes, with the newlines of the file."""
    assert load_prompt(name).encode('utf-8') == (PROMPTS_DIR / f'{name}.md').read_bytes()


def test_callers_use_the_loaded_text() -> None:
    """The names callers import are the loaded prompts, sent as they are (no format call on them)."""
    from server.routers.compare.config import DEFAULT_ANALYSIS_PROMPT
    from server.services.compare.processors._diff_engines import SYSTEM_PROMPT_STANDARD, SYSTEM_PROMPT_STRUCTURED
    from server.services.compare.summarize import SUMMARY_IMAGE_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
    from server.services.infra.vector_search import _JUDGE_SYSTEM_PROMPT
    assert DEFAULT_ANALYSIS_PROMPT == load_prompt('analysis_default')
    assert SYSTEM_PROMPT_STANDARD == load_prompt('analysis_standard')
    assert SYSTEM_PROMPT_STRUCTURED == load_prompt('analysis_structured')
    assert SUMMARY_SYSTEM_PROMPT == load_prompt('summary')
    assert SUMMARY_IMAGE_SYSTEM_PROMPT == load_prompt('summary_image')
    assert _JUDGE_SYSTEM_PROMPT == load_prompt('impact_judge')


def test_no_prompt_is_formatted_by_its_callers() -> None:
    """No caller calls ``.format`` on a compare prompt, so literal braces (JSON examples) are safe."""
    sources = [
        'server/routers/compare/config.py', 'server/services/compare/summarize.py',
        'server/services/infra/vector_search.py', 'server/services/compare/processors/text_diff.py',
        'server/services/compare/processors/excel.py', 'server/services/compare/processors/image.py',
    ]
    pattern = re.compile(r'(PROMPT|system_prompt|\bsp)\w*\.format\(')
    for rel in sources:
        assert not pattern.search((_REPO / rel).read_text(encoding='utf-8')), rel

