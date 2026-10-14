"""Offline DocCompare preprocessing report: no LLM call, no token spend. One-shot, run by hand from the repo root.

Runs the full extraction → diff → image-pairing → chunking pipeline on a local
document pair and logs what WOULD be sent to the model. Used to check engine
changes on real documents before deploying:

    python runbooks/scripts/compare/compare_preview.py old.docx new.docx
    python runbooks/scripts/compare/compare_preview.py old.pdf new.pdf --method structured --dump-diff diff.txt
    python runbooks/scripts/compare/compare_preview.py old.docx new.docx --threshold 0.45   # override the similarity threshold
"""

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

# Repo root is parents[3] of this file (runbooks/scripts/compare/): the server/ package is imported from there.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def _diff_stats(diff_text: str) -> str:
    mod = len(re.findall(r'^MODIFIED', diff_text, re.M))
    add = len(re.findall(r'^ADDED', diff_text, re.M))
    rem = len(re.findall(r'^REMOVED', diff_text, re.M))
    return f'{len(diff_text):,} chars | MODIFIED={mod} ADDED={add} REMOVED={rem}'


def _build_text_block(old: Path, new: Path, method: str) -> tuple[str, object]:
    # Import AFTER --threshold has had a chance to set the env var —
    # _diff_engines reads COMPARE_PAIR_RATIO_THRESHOLD at import time.
    from server.services.compare.processors.factory import get_processor
    processor = get_processor(old.name, method_override=method)
    if not processor:
        raise SystemExit(f'Unsupported file type: {old.suffix}')
    result = processor.build_messages(old.read_bytes(), old.name, new.read_bytes(), new.name)
    user_content = result.messages[-1]['content']
    text_block = user_content[0]['text'] if isinstance(user_content, list) else str(user_content)
    return text_block, result


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("compare_preview")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('old', type=Path, nargs='?')
    ap.add_argument('new', type=Path, nargs='?')
    ap.add_argument('--method', default='structured', choices=['standard', 'structured', 'comparative'])
    ap.add_argument('--chunk-threshold', type=int, default=60000)
    ap.add_argument('--chunk-size', type=int, default=45000)
    ap.add_argument('--dump-diff', type=Path, help='write the full text block to this file')
    ap.add_argument('--threshold', type=float,
                    help='override the 0.55 similarity threshold (COMPARE_PAIR_RATIO_THRESHOLD)')
    args = ap.parse_args()

    if args.threshold is not None:
        os.environ['COMPARE_PAIR_RATIO_THRESHOLD'] = str(args.threshold)

    if not args.old or not args.new:
        ap.error('old and new documents are required')

    from server.services.compare.chunked_analysis import split_messages_for_chunking

    t0 = time.perf_counter()
    text_block, result = _build_text_block(args.old, args.new, args.method)
    build_s = time.perf_counter() - t0

    user_content = result.messages[-1]['content']
    n_images = sum(1 for b in user_content if isinstance(b, dict) and b.get('type') == 'image_url') \
        if isinstance(user_content, list) else 0
    parts = split_messages_for_chunking(result.messages, args.chunk_threshold, args.chunk_size)

    logger.info(f'file_type={result.metadata.file_type} method={result.metadata.method} | build: {build_s:.1f}s')
    logger.info(f'text block : {_diff_stats(text_block)}')
    logger.info(f'images     : {n_images} block(s) sent to the LLM | {len(result.image_pairs)} UI pair(s): '
          + ', '.join(f"{p['status']}(p{p.get('old_page') or '?'}→p{p.get('new_page') or '?'})"
                      for p in result.image_pairs[:12]))
    logger.info(f'LLM calls  : {len(parts) if parts else 1} (chunk threshold {args.chunk_threshold:,})')
    for w in result.warnings:
        logger.warning(f'WARNING    : {w}')

    if args.dump_diff:
        args.dump_diff.write_text(text_block, encoding='utf-8')
        logger.info(f'text block written to {args.dump_diff}')


if __name__ == '__main__':
    main()
