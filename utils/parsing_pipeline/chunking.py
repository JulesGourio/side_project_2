"""chunking.py — splits a parsed document (markdown) into indexable passages.

Pure Python, no Spark/Docling import: unit-tested in tests/test_parsing_chunking.py and used
by utils.chunk_document (pipeline) and the DEV re-chunking notebook
(utils/databricks_ops/evaluation/rechunk_experiment.py).

Rules (audit docs/chat_vsi_audit_2026-10.md, § 5.2, P2–P9):
- the markdown is cut on headings of level 1–3 into sections; a passage never spans two
  sections of level 1 or 2 (P3) — except sections too small to stand alone (under a third of
  the minimum size), which join the next one;
- ``semantic_headers`` is the heading path COMMON to every block of the passage, so it is
  always a real lineage (P2); a block whose own path goes deeper gets a ``[sub > path]``
  line inside the passage, once, where it starts;
- the section path is written once, at the top of the passage (P5);
- consecutive passages of the same section overlap by the last lines of the previous one
  (``overlap_ratio`` of the target size, P4);
- sizes are bounded in tokens AND in characters (P8): runs of dots or underscores count
  few tokens for many characters;
- tables longer than the target are split by rows, the header repeated in each part;
- passages that are a table of contents or front matter (approval block, revision history)
  get their own ``chunk_content_type`` (P9) so the search can leave them out.
"""

import re
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Path = Tuple[Tuple[int, str], ...]          # ((level, title), ...)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DOT_LEADER_RE = re.compile(r"(\.{4,}|…{2,}|_{4,})\s*\d+\s*$", re.MULTILINE)
_TOC_TITLE_RE = re.compile(r"\b(sommaire|table des mati[eè]res|contents|table of contents|obsah|съдържание)\b",
                           re.IGNORECASE)
_FRONT_MATTER_RE = re.compile(
    r"(historique des (modifications|r[ée]visions|[ée]volutions|indices)|revision history|change history|"
    r"r[ée]dig[ée] par|v[ée]rifi[ée] par|approuv[ée] par|valid[ée] par|written by|checked by|approved by|"
    r"\bvisa\b|\bsignature\b|\bindice\s+[a-z]\b|\bdate d'application\b)",
    re.IGNORECASE)
NOISE_TYPES = ("toc", "front_matter", "boilerplate")


def default_count_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.5)) if text else 0


# ---------------------------------------------------------------------------
# Markdown -> blocks
# ---------------------------------------------------------------------------
def _is_table(block: str) -> bool:
    lines = block.split("\n")
    return len(lines) >= 2 and all(l.lstrip().startswith("|") for l in lines[:2]) and "---" in lines[1]


def markdown_blocks(text: str) -> List[Dict[str, Any]]:
    """[{'path': Path, 'text': str, 'table': bool}] in document order. Headings of level 1–3
    open a section and are not repeated in the text; deeper headings stay in the text."""
    blocks: List[Dict[str, Any]] = []
    path: List[Tuple[int, str]] = []
    buf: List[str] = []

    def flush():
        chunk = "\n".join(buf).strip()
        buf.clear()
        if not chunk:
            return
        for part in re.split(r"\n\s*\n", chunk):
            part = part.strip()
            if part:
                blocks.append({"path": tuple(path), "text": part, "table": _is_table(part)})

    for line in (text or "").split("\n"):
        m = _HEADING_RE.match(line.strip())
        if m and len(m.group(1)) <= 3:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, title))
        else:
            buf.append(line)
    flush()
    return blocks


# ---------------------------------------------------------------------------
# Oversized blocks
# ---------------------------------------------------------------------------
def _split_text(text: str, max_tokens: int, max_chars: int, count: Callable[[str], int]) -> List[str]:
    """Split on line breaks, then sentences, then spaces, then hard cuts."""
    if count(text) <= max_tokens and len(text) <= max_chars:
        return [text]
    for sep in ("\n", ". ", " "):
        parts = text.split(sep)
        if len(parts) < 2:
            continue
        out, cur = [], ""
        for p in parts:
            cand = p if not cur else cur + sep + p
            if cur and (count(cand) > max_tokens or len(cand) > max_chars):
                out.append(cur)
                cur = p
            else:
                cur = cand
        if cur:
            out.append(cur)
        if len(out) > 1:
            return [piece for o in out for piece in _split_text(o, max_tokens, max_chars, count)]
    step = max(1, max_chars)
    return [text[i:i + step] for i in range(0, len(text), step)]


def _split_table(text: str, max_tokens: int, max_chars: int, count: Callable[[str], int]) -> List[str]:
    lines = text.split("\n")
    head, rows = lines[:2], lines[2:]
    out, cur = [], list(head)
    for row in rows:
        cand = "\n".join(cur + [row])
        if len(cur) > 2 and (count(cand) > max_tokens or len(cand) > max_chars):
            out.append("\n".join(cur))
            cur = list(head)
        cur.append(row)
    if len(cur) > 2:
        out.append("\n".join(cur))
    # A single row over the limits is cut as text.
    return [piece for o in out for piece in
            (_split_text(o, max_tokens, max_chars, count) if count(o) > max_tokens * 2 or len(o) > max_chars * 2 else [o])]


# ---------------------------------------------------------------------------
# Blocks -> passages
# ---------------------------------------------------------------------------
def _common_path(paths: Sequence[Path]) -> Path:
    common = list(paths[0])
    for p in paths[1:]:
        n = 0
        while n < len(common) and n < len(p) and common[n] == p[n]:
            n += 1
        common = common[:n]
    return tuple(common)


def _section_key(path: Path) -> Path:
    return tuple(h for h in path if h[0] <= 2)


def _path_label(path: Path) -> str:
    return " > ".join(t for _, t in path)


def _render(blocks: List[Dict[str, Any]], overlap: str = "") -> Tuple[str, Path]:
    common = _common_path([b["path"] for b in blocks])
    lines = [f"[{_path_label(common)}]"] if common else []
    if overlap:
        lines.append(overlap)
    shown: Optional[Path] = common
    for b in blocks:
        if b["path"] != shown and len(b["path"]) > len(common):
            lines.append(f"[{_path_label(b['path'][len(common):])}]")
        shown = b["path"]
        lines.append(b["text"])
    return "\n\n".join(lines), common


def _tail(text: str, max_chars: int) -> str:
    """The last lines (or sentences) of text, at most max_chars, never a table fragment."""
    if max_chars <= 0 or text.lstrip().startswith("|"):
        return ""
    if len(text) <= max_chars:
        return text
    lines = [l for l in text.split("\n") if l.strip()]
    out = ""
    for line in reversed(lines):
        cand = line if not out else line + "\n" + out
        if len(cand) > max_chars:
            break
        out = cand
    if not out:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for s in reversed(sentences):
            cand = s if not out else s + " " + out
            if len(cand) > max_chars:
                break
            out = cand
    if not out:
        cut = text[-max_chars:]
        out = "…" + cut[cut.find(" ") + 1:] if " " in cut else cut
    return out


def classify(text: str, path: Path, base_type: str, index: int = 0) -> str:
    """toc / front_matter for passages that carry no content of their own, else base_type.
    Approval/revision words only count as front matter in the first 3 passages (a procedure
    section about approvals stays text)."""
    titles = " ".join(t for _, t in path)
    leaders = len(_DOT_LEADER_RE.findall(text))
    if leaders >= 3 or (_TOC_TITLE_RE.search(titles) and leaders >= 1):
        return "toc"
    hits = len(_FRONT_MATTER_RE.findall(text))
    if _FRONT_MATTER_RE.search(titles) or (hits >= 3 and index <= 2):
        return "front_matter"
    return base_type


def chunk_markdown(text: str, *, min_tokens: int = 250, target_tokens: int = 500, max_tokens: int = 1000,
                   max_chars: int = 4000, overlap_ratio: float = 0.12, chars_per_token: float = 3.5,
                   count_tokens: Callable[[str], int] = default_count_tokens) -> List[Dict[str, Any]]:
    """Passages of a markdown document: dicts with chunk_index, chunk_text, chunk_char_count,
    chunk_token_count, chunk_content_type, metadata (Header N -> title)."""
    count = count_tokens
    target_chars = min(max_chars, int(target_tokens * chars_per_token * 1.6))
    # 1. blocks, oversized ones split
    blocks: List[Dict[str, Any]] = []
    for b in markdown_blocks(text):
        if count(b["text"]) > target_tokens or len(b["text"]) > target_chars:
            split = (_split_table if b["table"] else _split_text)(b["text"], target_tokens, target_chars, count)
            blocks.extend({"path": b["path"], "text": s, "table": b["table"]} for s in split if s.strip())
        else:
            blocks.append(b)
    if not blocks:
        return []

    # 2. groups = consecutive blocks of one level-1/2 section; tiny sections join the next one
    groups: List[List[Dict[str, Any]]] = []
    for b in blocks:
        if groups and _section_key(groups[-1][-1]["path"]) == _section_key(b["path"]):
            groups[-1].append(b)
        else:
            groups.append([b])
    tiny = max(1, min_tokens // 3)
    merged: List[List[Dict[str, Any]]] = []
    carry: List[Dict[str, Any]] = []
    for i, g in enumerate(groups):
        g = carry + g
        carry = []
        if sum(count(b["text"]) for b in g) < tiny and i < len(groups) - 1:
            carry = g
            continue
        if sum(count(b["text"]) for b in g) < tiny and merged:   # tiny last section: joins the previous one
            merged[-1].extend(g)
            continue
        merged.append(g)
    if carry:
        if merged:
            merged[-1].extend(carry)
        else:
            merged.append(carry)

    # 3. pack each group, overlap inside a group only
    overlap_chars = int(target_tokens * overlap_ratio * chars_per_token)
    out: List[Dict[str, Any]] = []
    for g in merged:
        packs: List[List[Dict[str, Any]]] = []
        cur: List[Dict[str, Any]] = []
        for b in g:
            if cur:
                cand_text = "\n\n".join(x["text"] for x in cur + [b])
                cur_tokens = count("\n\n".join(x["text"] for x in cur))
                if cur_tokens >= target_tokens or count(cand_text) > max_tokens or len(cand_text) > max_chars:
                    packs.append(cur)
                    cur = []
            cur.append(b)
        if cur:
            if packs and count("\n\n".join(x["text"] for x in cur)) < min_tokens:
                cand = packs[-1] + cur
                cand_text = "\n\n".join(x["text"] for x in cand)
                if count(cand_text) <= max_tokens and len(cand_text) <= max_chars:
                    packs[-1] = cand
                    cur = []
            if cur:
                packs.append(cur)
        prev_last = ""
        for pack in packs:
            overlap = _tail(prev_last, overlap_chars) if prev_last else ""
            if overlap and overlap == pack[0]["text"]:
                overlap = ""
            body, common = _render(pack, overlap)
            tables = sum(1 for b in pack if b["table"])
            base = "table" if tables == len(pack) else "mixed" if tables else "text"
            out.append({
                "chunk_text": body, "chunk_char_count": len(body), "chunk_token_count": count(body),
                "chunk_content_type": classify(body, common, base, len(out)),
                "metadata": {f"Header {lvl}": title for lvl, title in common},
            })
            prev_last = pack[-1]["text"]
    for i, c in enumerate(out):
        c["chunk_index"] = i
    return out


def source_prefix(ref: str, titre: str, type_document: str, division: str, category: str,
                  doc_date: Any = None) -> str:
    """Python twin of utils.source_prefixed_text (Spark): the "[Source: …]" line embedded at
    the top of every passage."""
    date = doc_date.strftime("%Y-%m-%d") if hasattr(doc_date, "strftime") else (str(doc_date) if doc_date else "inconnue")
    return (f"[Source: {ref or ''} | Title: {titre or ''} | Type: {type_document or ''} | "
            f"Division: {division or ''} | Category: {category or ''} | Date de diffusion: {date}]\n\n")


# ---------------------------------------------------------------------------
# Corpus-level noise: the same passage body in many documents
# ---------------------------------------------------------------------------
def body_fingerprint(chunk_text: str) -> str:
    """Normalised body (section line removed, case/accents/spaces folded) used to find passages
    repeated across documents (legal mentions, standard approval blocks)."""
    body = re.sub(r"^\[[^\]]*\]\s*", "", chunk_text or "")
    body = unicodedata.normalize("NFKD", body).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\W+", " ", body).strip()


# ---------------------------------------------------------------------------
# Language of a document
# ---------------------------------------------------------------------------
_SUFFIX_LANG = {"FR": "fr", "EN": "en", "GB": "en", "BG": "bg", "CZ": "cs", "MX": "es", "ES": "es", "BR": "pt"}
_STOP = {
    "fr": " le la les des du est et pour dans une sur par avec que qui ce sont aux ".split(),
    "en": " the and of to is for in on with that by are this be as from ".split(),
    "es": " el los las del es y para en una con que por se al ".split(),
    "pt": " o os as do da é e para em uma com que por ao não ".split(),
    "cs": " a je na se že ve pro do jsou nebo ".split(),
}


def detect_language(text: str, ref: str = "") -> str:
    """ISO 639-1 code: from the REF language suffix when present (_GB, _FR…), else from
    stop words in the first 5,000 characters. 'und' when nothing fits."""
    m = re.search(r"[-_. ](FR|EN|GB|BG|CZ|MX|ES|BR)$", (ref or "").upper())
    if m:
        return _SUFFIX_LANG[m.group(1)]
    if re.search(r"[а-яА-Я]{4,}", text[:5000] or ""):
        return "bg"
    words = re.findall(r"[a-zà-ÿěščřžýáíéůú]+", (text or "")[:5000].lower())
    if not words:
        return "und"
    counts = {lang: sum(1 for w in words if w in set(stop)) for lang, stop in _STOP.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] >= 3 else "und"


# ---------------------------------------------------------------------------
# Spreadsheets
# ---------------------------------------------------------------------------
def header_row_index(rows: Sequence[Sequence[str]], scan: int = 10) -> int:
    """Index of the header row among the first non-empty rows: the first one that fills at
    least 60 % of the widest row (a title line or a logo cell above the table is skipped)."""
    head = [r for r in rows[:scan]]
    if not head:
        return 0
    widths = [sum(1 for v in r if str(v).strip()) for r in head]
    widest = max(widths) or 1
    for i, w in enumerate(widths):
        if w >= max(2, 0.6 * widest):
            return i
    return 0


def sheet_lines(rows: Sequence[Sequence[Any]]) -> List[str]:
    """Rows of one sheet as "- header: value, header: value" lines. Lines above the header row
    (sheet title, logo cell) are kept as plain text instead of becoming the header."""
    rows = [[("" if v is None else str(v)).strip() for v in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    h = header_row_index(rows)
    out = [" ".join(v for v in r if v) for r in rows[:h]]
    headers = rows[h]
    for r in rows[h + 1:]:
        parts = [f"{(headers[j] if j < len(headers) and headers[j] else f'col{j + 1}')}: {v}"
                 for j, v in enumerate(r) if v]
        if parts:
            out.append("- " + ", ".join(parts))
    return out


# ---------------------------------------------------------------------------
# Image passages
# ---------------------------------------------------------------------------
_IMAGE_MARK = "[... IMAGE INSERTED HERE ...]"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\W+", " ", s).strip()


def image_anchor(context_text: str, chunks: Sequence[Tuple[int, str, Dict[str, str]]]) -> Optional[Tuple[int, Dict[str, str]]]:
    """(chunk_index, headers) of the text passage where the image sits, found by the text just
    before the image (else just after) that Docling stored in image_metadata.context_text."""
    if not context_text or not chunks:
        return None
    before, _, after = context_text.partition(_IMAGE_MARK)
    probes = []
    lines_before = [l for l in before.split("\n") if len(_norm(l)) >= 12]
    lines_after = [l for l in after.split("\n") if len(_norm(l)) >= 12]
    if lines_before:
        probes.append(_norm(lines_before[-1])[-80:])
    if lines_after:
        probes.append(_norm(lines_after[0])[:80])
    normed = [(idx, _norm(txt), hdr) for idx, txt, hdr in chunks]
    for probe in probes:
        for idx, txt, hdr in normed:
            if probe and probe in txt:
                return idx, hdr
    return None


def image_passage_body(description: str, captions: Sequence[str] = (), headers: Optional[Dict[str, str]] = None) -> str:
    """Description of an image with the section it sits in and its caption, which the vision
    prompt keeps out of the description itself."""
    lines = []
    if headers:
        lines.append("Section : " + " > ".join(headers[k] for k in sorted(headers)))
    caps = [c.strip() for c in captions or [] if c and c.strip()]
    if caps:
        lines.append("Légende : " + " | ".join(caps))
    return ("\n".join(lines) + "\n\n" if lines else "") + (description or "").strip()


def split_long_description(text: str, max_chars: int = 4000, max_tokens: int = 1000,
                           count_tokens: Callable[[str], int] = default_count_tokens) -> List[str]:
    """A long transcription (scanned page) cut into passages; the first line (the
    '# [TYPE] what the page is' summary) is repeated at the top of each part."""
    text = (text or "").strip()
    if len(text) <= max_chars and count_tokens(text) <= max_tokens:
        return [text]
    first, _, rest = text.partition("\n")
    head = first if first.startswith("#") else ""
    body = rest if head else text
    room = max(500, max_chars - len(head) - 2)
    parts = []
    for block in [p for p in re.split(r"\n\s*\n", body) if p.strip()]:
        if parts and len(parts[-1]) + 2 + len(block) <= room and count_tokens(parts[-1] + block) <= max_tokens:
            parts[-1] += "\n\n" + block
        else:
            parts.extend(_split_text(block, max_tokens, room, count_tokens) if len(block) > room else [block])
    return [(head + "\n\n" + p) if head else p for p in parts]
