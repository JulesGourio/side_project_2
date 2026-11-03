"""The language the comparison report is written in: the language of the new document, named to the model."""

from server.services.chat.config import LANGUAGE_NAMES
from server.services.chat.translation_bridge import fast_lang_guess

_SAMPLE_CHARS = 3000

STRUCTURED_KEYS_NOTE = ' The JSON keys and the values of "type" and "criticality" stay in English.'


def detect_language(text: str) -> str | None:
    """The language of a document, from a sample of its beginning and its middle.

    :param text: text of the document
    :return: the ISO 639-1 code, or None when the detection is not confident or the language is not one of ``LANGUAGE_NAMES``
    """
    if not text:
        return None
    middle = len(text) // 2
    code = fast_lang_guess(f'{text[:_SAMPLE_CHARS]} {text[middle:middle + _SAMPLE_CHARS]}')
    return code if code in LANGUAGE_NAMES else None


def language_instruction(new_text: str, structured: bool = False) -> str:
    """The sentence that fixes the report language to the one of the new document.

    :param new_text: text of the new document
    :param structured: True when the report is a JSON array, whose keys and type values stay in English
    :return: the instruction, or an empty string when the language of the new document is not known with confidence
    """
    code = detect_language(new_text)
    if code is None:
        return ''
    note = STRUCTURED_KEYS_NOTE if structured else ''
    return (f'OUTPUT LANGUAGE: write the entire report in {LANGUAGE_NAMES[code]}, '
            f'whatever the language of the old document or of these instructions.{note}')


def with_language_instruction(system_prompt: str, new_text: str, structured: bool = False) -> str:
    """A system prompt closed by the language instruction of the new document.

    :param system_prompt: system prompt of the analysis
    :param new_text: text of the new document
    :param structured: True when the report is a JSON array
    :return: the prompt followed by the instruction, or the prompt unchanged when the language is not known with confidence
    """
    instruction = language_instruction(new_text, structured)
    return f'{system_prompt}\n\n{instruction}' if instruction else system_prompt
