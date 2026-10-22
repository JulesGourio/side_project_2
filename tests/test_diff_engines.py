"""Behaviour tests of the text and image diff engines, on small hand-written inputs."""

import base64
import io

import pytest
from PIL import Image, ImageDraw

from server.services.compare.processors._diff_engines import (
    dhash,
    hash16,
    image_diff_blocks_dual,
    image_diff_pairs,
    images_are_similar,
    is_substantive_keep_modals,
    paragraph_semantic_diff,
    section_canonical_diff,
)

SENTENCE = ('The supplier {modal} deliver the part within ten working days after approval of the drawing, '
            'and the tolerance on the bracket offset is {value} mm in all cases.')
LONG_TEXT = ('The operator shall record every torque value measured on the primary fastener assembly '
             'and verify the result against the drawing before delivery. ') * 2


def sentence(modal: str = 'shall', value: str = '40') -> str:
    """Build the reference sentence with a given modal verb and tolerance value.

    :param modal: modal verb (or verb phrase) placed after the subject
    :param value: tolerance value written before ``mm``
    :return: the sentence
    """
    return SENTENCE.format(modal=modal, value=value)


def toc(last_page_offset: int) -> str:
    """Build a table of contents of seven entries whose page numbers are shifted by an offset.

    :param last_page_offset: number added to the section number to get the page number
    :return: the table of contents, one entry per line
    """
    return '\n'.join(f'{i}. SECTION {i} ........ {i + last_page_offset}' for i in range(1, 8))


def patched(step: int, extra: int = -1) -> str:
    """Build 120 paragraphs over 12 pages with a repeated 'TO PATCH' line every ``step`` paragraphs.

    :param step: a 'TO PATCH' line follows every paragraph whose index is a multiple of this value
    :param extra: index of one more paragraph followed by a 'TO PATCH' line (-1 for none)
    :return: the text, one line per paragraph, each tagged with its page
    """
    lines = []
    for i in range(120):
        lines.append(f'Paragraph number {i} talks about thing{i} and other{i * 3} in detail today. [Page {i // 10 + 1}]')
        if i % step == 0 or i == extra:
            lines.append(f'TO PATCH [Page {i // 10 + 1}]')
    return '\n'.join(lines)


def diff(old: str, new: str, label: str | None = None) -> str:
    """Run the paragraph engine and return only its text.

    :param old: old document text
    :param new: new document text
    :param label: page label used by the extraction (``None`` when absent)
    :return: the diff text
    """
    return paragraph_semantic_diff(old, new, label)[0]


# --- paragraph diff: basic edits ---------------------------------------------------------------

def test_identical_texts_give_an_empty_diff():
    text = 'Hello world block one two three\nAnother block here today'
    assert paragraph_semantic_diff(text, text) == ('', 0)


def test_added_paragraph():
    old = 'First paragraph has several words here\nSecond paragraph has several words too'
    new = ('First paragraph has several words here\nInserted paragraph is brand new text\n'
           'Second paragraph has several words too')
    assert diff(old, new) == '\n## Preamble\nADDED: Inserted paragraph is brand new text'


def test_removed_paragraph():
    old = 'First paragraph has several words here\nSecond paragraph has several words too'
    new = 'First paragraph has several words here'
    assert diff(old, new) == '\n## Preamble\nREMOVED: Second paragraph has several words too'


def test_empty_old_text_is_all_added():
    assert diff('', 'New paragraph with several words inside it') == (
        '\n## Preamble\nADDED: New paragraph with several words inside it')


def test_empty_new_text_is_all_removed():
    assert diff('Old paragraph with several words inside it', '') == (
        '\n## Preamble\nREMOVED: Old paragraph with several words inside it')


def test_modified_paragraph_shows_inline_word_changes():
    old = 'The torque value is 45 Nm for the bracket assembly'
    new = 'The torque value is 50 Nm for the bracket assembly'
    assert diff(old, new) == '\n## Preamble\nMODIFIED: The torque value is ~~45~~ **50** Nm for the bracket assembly'


def test_changes_are_grouped_under_the_section_heading():
    old = '1. SCOPE\nUnchanged line with several words here\n2. OTHER\nAnother stable line with words'
    new = '1. SCOPE\nUnchanged line with several words here\n2. OTHER\nAnother stable line with words\nA brand new closing line appears'
    assert diff(old, new) == '\n## 2. OTHER\nADDED: A brand new closing line appears'


# --- paragraph diff: what counts as a substantive change ---------------------------------------

def test_number_change_in_a_long_paragraph_is_reported():
    assert diff(sentence(value='40'), sentence(value='45')) == (
        '\n## Preamble\nMODIFIED: ' + sentence(value='~~40~~ **45**'))


def test_sign_change_is_substantive():
    assert diff(sentence(value='-40'), sentence(value='40')) == (
        '\n## Preamble\nMODIFIED: ' + sentence(value='~~-40~~ **40**'))
    assert is_substantive_keep_modals(sentence(value='-40'), sentence(value='40'))


def test_negation_added_to_a_modal_is_reported():
    assert diff(sentence('shall'), sentence('shall not')) == (
        '\n## Preamble\nMODIFIED: The supplier shall **not** deliver' + sentence('shall')[len('The supplier shall deliver'):])


def test_modal_swap_across_obligation_groups_is_reported():
    out = diff(sentence('shall'), sentence('may'))
    assert out == '\n## Preamble\nMODIFIED: ' + sentence('~~shall~~ **may**')


def test_modal_swap_inside_one_obligation_group_is_filtered():
    assert paragraph_semantic_diff(sentence('shall'), sentence('must')) == ('', 1)
    assert not is_substantive_keep_modals(sentence('shall'), sentence('must'))


@pytest.mark.parametrize('new', [
    sentence().upper().replace('SHALL', 'shall') + '  ',
    sentence().replace('the part', 'part'),
    sentence().replace('The supplier', 'THE  supplier'),
])
def test_case_spacing_and_article_changes_are_ignored(new):
    assert diff(sentence(), new) == ''


def test_spelling_variant_is_ignored():
    old = 'The coating colour of the bracket shall be checked by the operator before the delivery to the customer site'
    new = old.replace('colour', 'color')
    assert paragraph_semantic_diff(old, new) == ('', 1)


def test_bullet_style_change_is_ignored():
    old = '- The torque value is 45 Nm for the bracket assembly'
    new = '• The torque value is 45 Nm for the bracket assembly'
    assert paragraph_semantic_diff(old, new) == ('', 1)


def test_page_tag_change_alone_is_not_a_change():
    old = 'The torque value is 45 Nm for the bracket assembly [Page 1]'
    new = 'The torque value is 45 Nm for the bracket assembly [Page 4]'
    assert diff(old, new) == ''


def test_table_of_contents_page_numbers_are_each_reported():
    out = diff(toc(3), toc(4))
    assert out.splitlines()[2] == 'MODIFIED: 1. SECTION 1 ........ ~~4~~ **5**'
    assert out.count('MODIFIED:') == 7


# --- moved text, collapse, continued sections --------------------------------------------------

def test_paragraphs_swapped_inside_the_document_are_not_reported():
    a = 'Alpha paragraph discusses fastener inspection procedure in some detail today.'
    b = 'Bravo paragraph covers torque tolerance measurement methods with calibrated tools.'
    c = 'Charlie paragraph lists supplier delivery obligations and approval timelines overall.'
    old = f'1. FIRST\n{a}\n2. SECOND\n{b}\n3. THIRD\n{c}'
    new = f'1. FIRST\n{b}\n2. SECOND\n{a}\n3. THIRD\n{c}'
    assert diff(old, new) == ''


def test_text_moved_to_another_section_is_not_reported_as_removed_and_added():
    old = f'1. SCOPE\n{LONG_TEXT}[Page 1]\n2. OTHER\nUnrelated short line here ok [Page 2]'
    new = f'1. SCOPE\nSomething else entirely different goes here now [Page 1]\n2. OTHER\n{LONG_TEXT}[Page 2]'
    assert diff(old, new) == (
        '\n## 1. SCOPE\nADDED: Something else entirely different goes here now\n\n'
        '## 2. OTHER\nREMOVED: Unrelated short line here ok')


def test_heading_repeated_with_continued_is_merged():
    old = ('3. INSPECTION\n' + LONG_TEXT + '\n3. INSPECTION (Continued)\n'
           'More text continues here after the break')
    new = '3. INSPECTION\n' + LONG_TEXT + 'More text continues here after the break'
    assert paragraph_semantic_diff(old, new) == ('', 3)


def test_line_added_on_many_pages_is_collapsed_into_one_entry():
    out = diff(patched(30), patched(28, extra=118), 'Page')
    assert out == ('\n## Preamble\n'
                   'ADDED [Page 1-12, repeated on 5 pages — recurring header/footer block]: TO PATCH\n'
                   'REMOVED [Page 7]: TO PATCH\nREMOVED [Page 10]: TO PATCH')


def test_same_word_substituted_everywhere_is_reported_once():
    old = '\n'.join(f'Paragraph {i} refers to the ANNEX section and the ANNEX list for details [Page {i}]'
                    for i in range(1, 8))
    out = diff(old, old.replace('ANNEX', 'APPENDIX'))
    assert out == ('\n## Preamble\nMODIFIED: Paragraph 1 refers to the ~~ANNEX~~ **APPENDIX** section and the '
                   "~~ANNEX~~ **APPENDIX** list for details [same substitution repeated in 6 more entries "
                   "— 'annex' -> 'appendix']")


# --- section engine ----------------------------------------------------------------------------

def test_section_diff_shows_removed_and_added_lines():
    old = '1. SCOPE\nThe torque value is 45 Nm for the bracket assembly\n2. OTHER\nUnchanged line with several words here'
    new = old.replace('45 Nm', '50 Nm')
    assert section_canonical_diff(old, new) == (
        '\n## 1. SCOPE\nREMOVED: The torque value is 45 Nm for the bracket assembly\n'
        'ADDED: The torque value is 50 Nm for the bracket assembly', 0)


def test_section_diff_reports_a_modal_swap_across_obligation_groups():
    old = '1. SCOPE\n' + sentence('shall')
    new = '1. SCOPE\n' + sentence('may')
    out, filtered = section_canonical_diff(old, new)
    assert filtered == 0 and 'REMOVED: ' + sentence('shall') in out and 'ADDED: ' + sentence('may') in out


def test_section_diff_filters_a_modal_swap_inside_one_group():
    old = '1. SCOPE\n' + sentence('shall')
    assert section_canonical_diff(old, '1. SCOPE\n' + sentence('must')) == ('', 1)


def test_section_diff_of_identical_texts_is_empty():
    assert section_canonical_diff('a b c d e f', 'a b c d e f') == ('', 0)


# --- images ------------------------------------------------------------------------------------

def png(mark: str | None = None) -> bytes:
    """Draw a small white image with an optional black mark.

    :param mark: ``'half'`` (left half black), ``'half_line'`` (left half black plus a thin diagonal line) or
        ``None`` (blank)
    :return: PNG bytes
    """
    img = Image.new('RGB', (64, 64), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    if mark in ('half', 'half_line'):
        draw.rectangle([0, 0, 31, 63], fill=(0, 0, 0))
    if mark == 'half_line':
        draw.line([0, 0, 63, 63], fill=(0, 0, 0), width=1)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def entry(raw: bytes, page: int, **extra) -> dict:
    """Build the one-image dict in the extractors' format ``{dhash: metadata}``.

    :param raw: image bytes
    :param page: page the image appears on
    :param extra: more metadata (``transform``, ``raw_hash``)
    :return: a dict with a single entry
    """
    meta = {'b64': base64.b64encode(raw).decode('ascii'), 'pages': [page], 'hash16': hash16(raw), **extra}
    return {dhash(raw): meta}


def block_texts(blocks: list) -> list:
    """Keep the text blocks of an image diff and replace each image block by ``'IMAGE'``.

    :param blocks: blocks returned by ``image_diff_blocks_dual``
    :return: the texts, with ``'IMAGE'`` for the image blocks
    """
    return [b['text'] if b['type'] == 'text' else 'IMAGE' for b in blocks]


def test_identical_image_sets_report_nothing():
    imgs = entry(png('half'), 1)
    assert block_texts(image_diff_blocks_dual(imgs, imgs, True)) == ['\nNo embedded images added, removed, or modified.']
    assert image_diff_pairs(imgs, imgs, True) == []


def test_no_images_on_either_side_reports_nothing():
    assert block_texts(image_diff_blocks_dual({}, {}, False)) == ['\nNo embedded images added, removed, or modified.']


def test_added_image_with_page_position():
    assert block_texts(image_diff_blocks_dual({}, entry(png('half'), 3), True)) == ['ADDED image (page 3):', 'IMAGE']
    assert block_texts(image_diff_blocks_dual({}, entry(png('half'), 3), False)) == ['ADDED image:', 'IMAGE']


def test_removed_image_pair_carries_the_old_page():
    pairs = image_diff_pairs(entry(png('half'), 4), {}, True)
    assert [(p['status'], p['old_page'], p['new_page']) for p in pairs] == [('removed', 4, None)]


def test_slightly_edited_image_is_paired_as_modified():
    old, new = entry(png('half'), 2), entry(png('half_line'), 2)
    texts = block_texts(image_diff_blocks_dual(old, new, True))
    assert texts[0] == '\n1 image(s) MODIFIED (OLD → NEW):'
    assert texts[1].startswith('Page 2 → 2 | hamming=')
    assert texts[2:] == ['IMAGE', 'NEW IMAGE:', 'IMAGE']
    pairs = image_diff_pairs(old, new, True)
    assert [(p['status'], p['old_page'], p['new_page']) for p in pairs] == [('modified', 2, 2)]


def test_unrelated_images_are_removed_and_added_not_modified():
    old, new = entry(png('half'), 1), entry(png(), 1)
    assert not images_are_similar(next(iter(old)), next(iter(new)))
    assert block_texts(image_diff_blocks_dual(old, new, False)) == ['REMOVED image:', 'IMAGE', 'ADDED image:', 'IMAGE']


def test_flipped_image_is_reported_as_orientation_change():
    raw = png('half')
    old = entry(raw, 1, transform=(False, False), raw_hash='r1')
    new = entry(raw, 1, transform=(True, False), raw_hash='r1')
    assert block_texts(image_diff_blocks_dual(old, new, True)) == [
        '\n1 image(s) ORIENTATION CHANGED (flipped/rotated):', 'flipH: False → True | IMAGE (new orientation):', 'IMAGE']
    assert [p['status'] for p in image_diff_pairs(old, new, True)] == ['orientation_changed']
