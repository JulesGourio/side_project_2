"""System prompts of the standard (Markdown) and structured (JSON for Excel) analysis methods."""

from server.services.compare.prompts import load_prompt

SYSTEM_PROMPT_STANDARD = load_prompt('analysis_standard')

SYSTEM_PROMPT_STRUCTURED = load_prompt('analysis_structured')
