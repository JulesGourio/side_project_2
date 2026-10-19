"""Page-number estimation for a DOCX, which has none without a rendering engine."""

from typing import Any, Dict, List, Tuple

from server.services.compare.processors.docx.ooxml import _W_BR, _W_LRPB, _W_TYPE

# --- Page-number estimation ---
# No rendering engine: pages are estimated from the page-break markers plus a content-flow estimate.
# _PAGE_WEIGHT_CAP: flow weight that fills one page (text characters + _IMG_WEIGHT per image); overlay content weighs 0.
# _IMG_WEIGHT: vertical space of one embedded image, in characters.
_PAGE_WEIGHT_CAP = 2200
_IMG_WEIGHT = 400


def _count_breaks_split(element) -> Tuple[int, int]:
    """Return (explicit_breaks, lastRendered_breaks) separately for one element.

    Explicit <w:br w:type="page"/> are author-forced and always reliable.
    <w:lastRenderedPageBreak/> are Word's cached render boundaries — accurate
    only when the file was freshly saved by Word, stale/partial otherwise.
    Keeping them separate lets the caller drop the lastRendered ones when they
    look stale (see _lrpb_is_stale).
    """
    forced = 0
    lrpb = 0
    for node in element.iter():
        if node.tag == _W_BR and node.get(_W_TYPE) == 'page':
            forced += 1
        elif node.tag == _W_LRPB:
            lrpb += 1
    return forced, lrpb


def _lrpb_is_stale(total_forced: int, total_lrpb: int) -> bool:
    """Decide whether <w:lastRenderedPageBreak/> markers should be ignored.

    On a freshly rendered document Word emits a lastRenderedPageBreak at every page boundary, including just before
    each forced break, so the count is normally >= the number of forced breaks. When it is much smaller, the document
    was edited after its last full render: the markers are a stale subset that would inflate the page count, so they
    are discarded in favour of forced breaks and content flow.
    """
    return total_lrpb < total_forced * 0.5


def _assign_pages(entries: List[Dict[str, Any]], use_lrpb: bool) -> None:
    """Assign a 1-based page number to each entry, in place (sets entry['page']).

    entries is the document-ordered list produced by the body walk. Each entry
    carries:
        weight : flow weight (0 for overlay content — text boxes / VML arrows)
        forced : explicit page breaks occurring just before this entry
        lrpb   : lastRenderedPageBreaks occurring just before this entry

    A new page starts on every hard break (forced, plus lastRendered when not
    stale) and, between breaks, whenever accumulated flow weight would exceed
    _PAGE_WEIGHT_CAP. The weight rule paginates marker-poor documents (long text
    files with only a couple of explicit breaks) without disturbing marker-rich
    ones, where breaks fire first and keep the accumulator small.
    """
    page = 1
    acc = 0
    for e in entries:
        hard = e['forced'] + (e['lrpb'] if use_lrpb else 0)
        if hard:
            page += hard
            acc = 0
        w = e['weight']
        if acc > 0 and w > 0 and acc + w > _PAGE_WEIGHT_CAP:
            page += 1
            acc = 0
        e['page'] = page
        acc += w
