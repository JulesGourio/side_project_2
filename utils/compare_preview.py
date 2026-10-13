"""Offline DocCompare preprocessing report — no LLM call, no token spend.

Runs the full extraction → diff → image-pairing → chunking pipeline on a local
document pair and prints what WOULD be sent to the model. Use it to validate
engine changes on real documents before deploying:

    python -m utils.compare_preview old.docx new.docx
    python -m utils.compare_preview old.pdf new.pdf --method structured --dump-diff diff.txt

Scoring mode — recall/precision of the deterministic diff against a human
reference (utils/compare_eval/refs/*.json, see utils/compare_eval/README.md).
Every engine change becomes measurable, still without a single token:

    python -m utils.compare_preview --score utils/compare_eval/refs/NE07-011.json
    python -m utils.compare_preview --score utils/compare_eval/refs/NE07-011.json --threshold 0.45

Exit code 0 always (diagnostic tool); errors are printed.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DIFF_KINDS = ('MODIFIED', 'ADDED', 'REMOVED')


def _diff_stats(diff_text: str) -> str:
    mod = len(re.findall(r'^MODIFIED', diff_text, re.M))
    add = len(re.findall(r'^ADDED', diff_text, re.M))
    rem = len(re.findall(r'^REMOVED', diff_text, re.M))
    return f'{len(diff_text):,} chars | MODIFIED={mod} ADDED={add} REMOVED={rem}'


def _build_text_block(old: Path, new: Path, method: str) -> tuple[str, object]:
    # Import AFTER --threshold has had a chance to set the env var —
    # _diff_engines reads COMPARE_PAIR_RATIO_THRESHOLD at import time.
    from server.services.processors.factory import get_processor
    processor = get_processor(old.name, method_override=method)
    if not processor:
        raise SystemExit(f'Unsupported file type: {old.suffix}')
    result = processor.build_messages(old.read_bytes(), old.name, new.read_bytes(), new.name)
    user_content = result.messages[-1]['content']
    text_block = user_content[0]['text'] if isinstance(user_content, list) else str(user_content)
    return text_block, result


def parse_diff_entries(diff_text: str) -> list[dict]:
    """Split the diff text block into one entry per MODIFIED/ADDED/REMOVED
    block (an entry runs until the next block start or a blank line run)."""
    entries: list[dict] = []
    current: dict | None = None
    for line in diff_text.splitlines():
        m = re.match(r'^(MODIFIED|ADDED|REMOVED)\b', line)
        if m:
            current = {'kind': m.group(1), 'text': line}
            entries.append(current)
        elif current is not None and line.strip():
            current['text'] += '\n' + line
        elif current is not None and not line.strip():
            current = None
    return entries


def score_against_reference(diff_text: str, ref: dict) -> dict:
    """Recall/precision of the diff entries against the reference list.

    An expected change matches an entry when the kinds agree ('*' matches
    any) and every `must_contain` string appears (case-insensitive) in the
    entry text. Precision counts entries claimed by at least one expected
    change — the reference lists the changes that MATTER, so precision is a
    lower bound whenever the reference is not exhaustive."""
    entries = parse_diff_entries(diff_text)
    matched_entries: set[int] = set()
    results = []
    for exp in ref.get('expected_changes', []):
        needles = [s.casefold() for s in exp.get('must_contain', [])]
        kind = exp.get('kind', '*')
        hit = None
        for i, e in enumerate(entries):
            if kind not in ('*', e['kind']):
                continue
            text = e['text'].casefold()
            if all(n in text for n in needles):
                hit = i
                break
        if hit is not None:
            matched_entries.add(hit)
        results.append({'id': exp.get('id', '?'), 'matched': hit is not None,
                        'note': exp.get('note', '')})

    noise_hits = []
    for noise in ref.get('must_not_flag', []):
        needles = [s.casefold() for s in noise.get('must_contain', [])]
        kind = noise.get('kind', '*')
        hits = [e for e in entries
                if kind in ('*', e['kind']) and all(n in e['text'].casefold() for n in needles)]
        # One collapsed, explicitly-annotated occurrence is fine — noise means
        # the motif is PRESENTED repeatedly (threshold aligned with the diff
        # engine's _BOILERPLATE_MIN_PAGES collapse). A motif that must NEVER
        # appear — a fabricated removal of content present in both revisions —
        # sets its own "max_hits": 0 instead.
        if len(hits) > noise.get('max_hits', 2):
            noise_hits.append({'id': noise.get('id', '?'), 'note': noise.get('note', ''),
                               'count': len(hits), 'entry': hits[0]['text'][:120]})

    n_expected = len(results)
    n_matched = sum(1 for r in results if r['matched'])
    return {
        'entries': len(entries),
        'expected': n_expected,
        'matched': n_matched,
        'recall': n_matched / n_expected if n_expected else None,
        'precision': len(matched_entries) / len(entries) if entries else None,
        'per_expected': results,
        'noise_hits': noise_hits,
    }


def run_score(ref_path: Path, method: str, dump_diff: Path | None) -> None:
    ref = json.loads(ref_path.read_text(encoding='utf-8'))
    base = ref_path.parent.parent  # refs/ -> compare_eval/
    old = (base / ref['old']).resolve()
    new = (base / ref['new']).resolve()
    if not old.exists() or not new.exists():
        print(f'{ref_path.name}: document pair not found locally — run '
              f'python -m utils.compare_eval.fetch_pairs first ({old.name}, {new.name})')
        return
    t0 = time.perf_counter()
    diff_text, _ = _build_text_block(old, new, method)
    s = score_against_reference(diff_text, ref)
    thr = os.getenv('COMPARE_PAIR_RATIO_THRESHOLD', '0.55')
    rec = f"{s['recall']:.0%}" if s['recall'] is not None else 'n/a'
    prec = f"{s['precision']:.0%}" if s['precision'] is not None else 'n/a'
    print(f"{ref.get('name', ref_path.stem)} [threshold={thr}] — "
          f"recall {s['matched']}/{s['expected']} ({rec}) | "
          f"precision {prec} of {s['entries']} entries | {time.perf_counter() - t0:.1f}s")
    for r in s['per_expected']:
        mark = 'OK  ' if r['matched'] else 'MISS'
        print(f"  {mark} {r['id']}: {r['note']}")
    for n in s['noise_hits']:
        print(f"  NOISE {n['id']} (x{n['count']}): {n['note']} -> {n['entry']!r}")
    if dump_diff:
        dump_diff.write_text(diff_text, encoding='utf-8')
        print(f'  diff written to {dump_diff}')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('old', type=Path, nargs='?')
    ap.add_argument('new', type=Path, nargs='?')
    ap.add_argument('--method', default='structured', choices=['standard', 'structured', 'comparative'])
    ap.add_argument('--chunk-threshold', type=int, default=60000)
    ap.add_argument('--chunk-size', type=int, default=45000)
    ap.add_argument('--dump-diff', type=Path, help='write the full text block to this file')
    ap.add_argument('--score', type=Path, action='append',
                    help='reference JSON (utils/compare_eval/refs/*.json) — repeatable')
    ap.add_argument('--threshold', type=float,
                    help='override the 0.55 similarity threshold (COMPARE_PAIR_RATIO_THRESHOLD)')
    args = ap.parse_args()

    if args.threshold is not None:
        os.environ['COMPARE_PAIR_RATIO_THRESHOLD'] = str(args.threshold)

    if args.score:
        for ref_path in args.score:
            run_score(ref_path, args.method, args.dump_diff)
        return

    if not args.old or not args.new:
        ap.error('old and new documents are required (or use --score)')

    from server.services.chunked_analysis import split_messages_for_chunking

    t0 = time.perf_counter()
    text_block, result = _build_text_block(args.old, args.new, args.method)
    build_s = time.perf_counter() - t0

    user_content = result.messages[-1]['content']
    n_images = sum(1 for b in user_content if isinstance(b, dict) and b.get('type') == 'image_url') \
        if isinstance(user_content, list) else 0
    parts = split_messages_for_chunking(result.messages, args.chunk_threshold, args.chunk_size)

    print(f'file_type={result.metadata.file_type} method={result.metadata.method} | build: {build_s:.1f}s')
    print(f'text block : {_diff_stats(text_block)}')
    print(f'images     : {n_images} block(s) sent to the LLM | {len(result.image_pairs)} UI pair(s): '
          + ', '.join(f"{p['status']}(p{p.get('old_page') or '?'}→p{p.get('new_page') or '?'})"
                      for p in result.image_pairs[:12]))
    print(f'LLM calls  : {len(parts) if parts else 1} (chunk threshold {args.chunk_threshold:,})')
    for w in result.warnings:
        print(f'WARNING    : {w}')

    if args.dump_diff:
        args.dump_diff.write_text(text_block, encoding='utf-8')
        print(f'text block written to {args.dump_diff}')


if __name__ == '__main__':
    main()
