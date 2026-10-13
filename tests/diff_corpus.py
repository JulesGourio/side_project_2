"""Deterministic synthetic documents that exercise the branches of the text diff engines.

test_diff_golden.py compares the engines' output on these inputs against
tests/golden/diff_cases.json, so refactors must keep them byte-identical.
"""

import random
from typing import Dict, List, Tuple

_WORDS = (
    'inspection torque fastener bracket assembly harness connector shall must may '
    'verify record procedure titanium steel aluminium coating thickness tolerance '
    'operator certified training program examination method report delivery supplier '
    'drawing revision approval ISO 9001 EN 9100 ASTM 1234 mm kg psi 12 45 7.5 100'
).split()

Block = Tuple[str, int, str]  # (kind, page, text); kind in head/para/footer


def _sentence(rng: random.Random, n: int) -> str:
    return ' '.join(rng.choice(_WORDS) for _ in range(n)).capitalize() + '.'


def _build(rng: random.Random, n_sections: int, per_section: int) -> List[Block]:
    blocks: List[Block] = []
    for page in range(1, n_sections + 1):
        blocks.append(('head', page, f'{page}. {rng.choice(_WORDS).upper()} {rng.choice(_WORDS).upper()}'))
        for _ in range(per_section):
            blocks.append(('para', page, _sentence(rng, rng.randint(2, 30))))
        blocks.append(('footer', page, f'PAGE : {page}/{n_sections * 2}'))
        blocks.append(('para', page, 'ANNEXE 4'))
    return blocks


def _render(blocks: List[Block]) -> str:
    return '\n'.join(f'{text} [Page {page}, Para {i}]' for i, (_k, page, text) in enumerate(blocks))


def make_pair(seed: int) -> Tuple[str, str]:
    rng = random.Random(seed)
    old = _build(rng, 8, 6)
    new = list(old)
    for _ in range(10):
        i = rng.randrange(len(new))
        kind, page, text = new[i]
        words = text.split()
        if kind == 'para' and len(words) > 4:
            words[rng.randrange(len(words))] = rng.choice(_WORDS)
            new[i] = (kind, page, ' '.join(words))
    for _ in range(4):
        del new[rng.randrange(len(new))]
    for _ in range(4):
        new.insert(rng.randrange(len(new)), ('para', 3, _sentence(rng, rng.randint(3, 20))))
    moved = new.pop(rng.randrange(len(new)))
    new.insert(rng.randrange(len(new)), moved)
    for old_modal, new_modal in ((' shall ', ' must '), (' must ', ' may ')):
        for i, (kind, page, text) in enumerate(new):
            if kind == 'para' and old_modal in text and len(text.split()) > 6:
                new[i] = (kind, page, text.replace(old_modal, new_modal, 1))
                break
    new = [(k, p, t.replace('/16', '/17') if k == 'footer' else t) for k, p, t in new]
    for i in [i for i, (k, _p, t) in enumerate(new) if k == 'para' and len(t.split()) > 5][:7]:
        k, p, t = new[i]
        new[i] = (k, p, t.replace(t.split()[0], 'APPENDIX', 1))
    new.insert(10, ('head', 2, '2. TORQUE FASTENER (Continued)'))
    return _render(old), _render(new)


def edge_cases() -> Dict[str, Tuple[str, str]]:
    long_text = ('The operator shall record every torque value measured on the primary '
                 'fastener assembly and verify the result against the drawing before delivery. ')
    supplier = 'The supplier {} deliver the part within ten working days after approval of the drawing.'
    def patched(step: int, extra: int = 0) -> str:
        lines = []
        for i in range(120):
            lines.append(f'Paragraph number {i} talks about {_WORDS[i % 30]} and {_WORDS[(i + 7) % 30]}'
                         f' in detail today. [Page {i // 10 + 1}]')
            if i % step == 0 or i == extra:
                lines.append(f'TO PATCH [Page {i // 10 + 1}]')
        return '\n'.join(lines)

    patch_old, patch_new = patched(30), patched(28, extra=118)
    stamp_old = '\n'.join(f'Revision ALPHA classification field value [Page {p}]' for p in range(1, 6))
    stamp_new = '\n'.join(f'Revision BRAVO classification field value [Page {p}]' for p in range(1, 6))
    annex_old = '\n'.join(
        f'Paragraph {i} refers to the ANNEX section and the ANNEX list for details [Page {i}]'
        for i in range(1, 8))
    annex_new = annex_old.replace('ANNEX', 'APPENDIX')
    moved_old = '1. A\nThe operator shall keep ' + long_text * 2 + 'and sign the page [Page 1]\n'
    moved_new = '1. A\nThe operator shall sign the page [Page 1]\n2. B\n' + long_text * 2 + '[Page 2]'
    return {
        'duplicate_labels': (patch_old, patch_new),
        'page_repeats': (stamp_old, stamp_new),
        'systemic_substitution': (annex_old, annex_new),
        'modified_with_relocated_span': (moved_old, moved_new),
        'identical': ('Hello world block one two three\nAnother block here today',) * 2,
        'empty_old': ('', 'New paragraph with several words inside it'),
        'empty_new': ('Old paragraph with several words inside it', ''),
        'relocation': (
            f'1. SCOPE\n{long_text * 2}[Page 1]\n2. OTHER\nUnrelated short line here ok [Page 2]',
            f'1. SCOPE\nSomething else entirely different goes here now [Page 1]\n2. OTHER\n{long_text * 2}[Page 2]',
        ),
        'toc': (
            '\n'.join(f'{i}. SECTION {i} ........ {i + 3}' for i in range(1, 8)),
            '\n'.join(f'{i}. SECTION {i} ........ {i + 4}' for i in range(1, 8)),
        ),
        'modal_cross_group': (supplier.format('shall'), supplier.format('may')),
        'modal_same_group': (supplier.format('shall'), supplier.format('must')),
        'row_key': (
            'Torque value check: col a: 1 | col b: 2 [Page 1]',
            'Torque value check: col a: 1 | col b: 2 | col c: totally new content here [Page 1]',
        ),
        'continued': (
            '3. INSPECTION\n' + long_text + '\n3. INSPECTION (Continued)\nMore text continues here after the break',
            '3. INSPECTION\n' + long_text + 'More text continues here after the break',
        ),
    }


def snapshot(paragraph_semantic_diff, section_canonical_diff, image_diff_blocks_dual,
             image_diff_pairs, dhash, hash16) -> dict:
    """Run every engine over the corpus; long outputs are stored as (sha256, length, filtered)."""
    import hashlib
    import json

    def digest(text: str, filtered: int):
        return [hashlib.sha256(text.encode()).hexdigest(), len(text), filtered]

    cases: dict = {}
    pairs = {f'seed{s}': make_pair(s) for s in range(1, 9)}
    pairs.update(edge_cases())
    for name, (old, new) in pairs.items():
        small = not name.startswith('seed')
        for label in (None, 'Page'):
            text, filtered = paragraph_semantic_diff(old, new, label)
            cases[f'para_{name}_{label}'] = [text, filtered] if small else digest(text, filtered)
        text, filtered = section_canonical_diff(old, new)
        cases[f'section_{name}'] = [text, filtered] if small else digest(text, filtered)

    def strip_b64(blocks):
        return [b['text'] if b['type'] == 'text' else 'IMAGE' for b in blocks]

    for name, (old, new) in image_cases(dhash, hash16).items():
        for show_position in (True, False):
            key = f'img_{name}_{show_position}'
            cases[f'{key}_blocks'] = strip_b64(image_diff_blocks_dual(old, new, show_position))
            cases[f'{key}_pairs'] = [
                {k: v for k, v in p.items() if k not in ('old_b64', 'new_b64')}
                for p in image_diff_pairs(old, new, show_position)
            ]
    return json.loads(json.dumps(cases))


def image_cases(dhash, hash16) -> Dict[str, Tuple[dict, dict]]:
    """(old_imgs, new_imgs) in the extractors' format: {dhash: {b64, pages, hash16, ...}}."""
    import base64
    import io

    from PIL import Image, ImageDraw

    def make(seed: int, edit: bool = False) -> bytes:
        rng = random.Random(seed)
        img = Image.new('RGB', (96, 96), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for _ in range(6):
            x0, y0 = rng.randrange(60), rng.randrange(60)
            draw.rectangle([x0, y0, x0 + rng.randrange(10, 36), y0 + rng.randrange(10, 36)],
                           fill=(rng.randrange(256),) * 3)
        if edit:
            draw.line([0, 0, 95, 95], fill=(0, 0, 0), width=3)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=90)
        return buf.getvalue()

    def entry(raw: bytes, page: int, **extra) -> Tuple[str, dict]:
        meta = {'b64': base64.b64encode(raw).decode('ascii'), 'pages': [page], 'hash16': hash16(raw)}
        meta.update(extra)
        return dhash(raw), meta

    a, b, c, d = make(1), make(2), make(3), make(4)
    return {
        'identical': (dict([entry(a, 1), entry(b, 2)]), dict([entry(a, 1), entry(b, 2)])),
        'modified_and_added': (
            dict([entry(a, 1), entry(b, 5)]),
            dict([entry(a, 1), entry(make(2, edit=True), 5), entry(c, 9)]),
        ),
        'removed': (dict([entry(a, 1), entry(d, 3)]), dict([entry(a, 1)])),
        'orientation': (
            dict([entry(a, 1, transform=(False, False), raw_hash='r1')]),
            dict([entry(a, 1, transform=(True, False), raw_hash='r1')]),
        ),
        'empty': ({}, {}),
    }
