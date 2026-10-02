"""Experimental prompt variants for the structured (Excel) method.

PRODUCTION CODE STILL USES :mod:`_diff_engines.SYSTEM_PROMPT_STRUCTURED`.
Nothing here is imported by the application; the variants are loaded only by
the debug notebook and the prompt-comparison test script.

Each variant differs from the baseline ONLY in:
  * the ``before`` / ``after`` / ``rationale`` field descriptions in the JSON
    schema preamble,
  * the trailing QUALITY section that tells the LLM how verbose to be.

Everything else — type taxonomy, criticality rules, equivalences, skip list —
is identical to the production prompt, so any behaviour difference comes from
the verbosity guidance, not from a different change taxonomy.

The three variants explore three different strategies for the
"too long for nothing / too short to be useful" problem:

* **A — Strict budgets per criticality.**  Hard word limits per field that
  scale with criticality.  Low-criticality rows are forced concise (Rationale
  empty), High-criticality rows allow longer verbatim quotes.  Mechanical and
  predictable.

* **B — Information density.**  No fixed word budget; instruct the LLM to
  output the *minimum necessary* information.  Rationale is filled in only
  when the change is not self-evident from the before/after columns.

* **C — Outcome / action driven.**  Quote only the substance of the change.
  Rationale answers a single question: what concrete action is required?
  If no action is triggered, rationale is empty.

* **D — C plus minor-wording suppression.**  Identical to C (production) but
  explicitly skips same-value / same-meaning rows: numbers rendered as digits
  vs spelled out ("24 months" = "twenty-four (24) months"), document dates, and
  synonym / word-order swaps. Targets the noise seen on the NAS 410 sample.

The variants are exposed both as standalone strings and via :func:`get_variant`.
"""

# ---------------------------------------------------------------------------
# Common rules — identical across baseline and all variants
# ---------------------------------------------------------------------------

_TYPES_AND_CRITICALITY = """\
TYPES — copy verbatim into the "type" field:
  Value changed        -- tolerance, limit, threshold, quantity, or duration changed
  Reference updated    -- normative document added, removed, or different revision cited
  Requirement modified -- existing obligation or specification rephrased with a meaning change
  Requirement added    -- new content (obligation, specification, note, warning, or definition) with no predecessor
  Requirement removed  -- existing content entirely eliminated with no replacement
  Procedure changed    -- process step, test method, inspection sequence, or criterion changed
  Scope changed        -- applicability, exception, inclusion, or exclusion changed
  Modal change         -- obligation level crossed between permission and mandatory
  Visual change        -- an embedded image, figure, diagram, chart, or illustration was added, removed, or visually modified
  Editorial/Structural -- section renumbering, text relocation, or Table of Contents (TOC) updates

CRITICALITY — evaluate in order; assign the FIRST level whose criteria are met:

  HIGH — stop here if ANY of the following applies:
    • A numeric value changed: tolerance, limit, threshold, quantity, or duration
    • A requirement was REMOVED (Note: relocating content or updating a TOC is NOT a removal)
    • Obligation degraded: shall/must/will → may/can/could (mandatory became optional)
    • Obligation raised: may/can/could → shall/must/will (optional became mandatory)
    • Scope NARROWED: fewer items covered, exception added, applicability restricted
    • A safety or airworthiness normative document changed revision or was removed
    • A normative reference changed revision, was removed, or newly added
    • A safety-critical figure, diagram, or technical illustration was modified or removed

  MEDIUM — use if NO High criterion applies, and ANY of the following applies:
    • A procedure or test method step changed (how work is performed)
    • A requirement was MODIFIED: meaning changed, obligation level unchanged
    • Scope EXPANDED: more items covered, exception removed
    • A new requirement, specification, or procedure added with no predecessor
    • A figure, diagram, or illustration was modified in a way that affects how work is performed

  LOW — use for everything else:
    • Structural changes: renumbering, content relocation, or Table of Contents (TOC) updates
    • Change adds clarity, minor detail, or editorial wording with limited compliance risk
    • A decorative or cosmetic image changed with no technical impact
    • ALWAYS use Low when in doubt — include the row, do not omit it
"""

_DIFF_AND_RULES = """\
DIFF FORMAT:
  ADDED / REMOVED: text completely new or gone.
  MODIFIED: ~~strikethrough~~ = old text; **bold** = new text; plain = unchanged context.
  A MODIFIED block is ONE evolved element — do NOT split into separate added + removed rows.

RULE FOR ADDED BLOCKS: An ADDED block is entirely new content. ALWAYS create a row for it
  unless it is clearly document metadata (see SKIP below). Use "before": "--".
  Default type: "Requirement added". Adjust to a more specific type if the content clearly
  fits (e.g. "Reference updated", "Scope changed", or "Editorial/Structural" for relocated text).
  Do NOT skip an ADDED block just because it seems like a minor addition — use Low criticality.

RULE FOR REMOVED BLOCKS: A REMOVED block is content that no longer exists. ALWAYS create a
  row for it unless it is clearly document metadata. Use "after": "--".
  Default type: "Requirement removed". High criticality ONLY if an actual obligation or specification was permanently eliminated, NOT if a section was just moved or a TOC updated.

RULE FOR VISUAL CHANGES (images in the diff): The section "--- VISUAL CHANGES ---" contains
  embedded images from the document. Examine each image carefully and compare OLD vs NEW.
  For every image that is MODIFIED, ADDED, or REMOVED, create a JSON row:
    - type: "Visual change"
    - section: the nearest document section title, or "Figures" if unknown
    - before / after / rationale: follow the BEFORE / AFTER / RATIONALE rules of this prompt
    - criticality: High if it is a safety diagram or changes technical specifications;
                   Medium if it affects a procedure, sequence diagram, or technical drawing;
                   Low if cosmetic or decorative only
  Do NOT skip visual changes. If no images changed, skip this rule.

IMPORTANT — VML shape metadata: Lines like `[Arrow "id" left: ... top: ...]` and
  `[Line "id" ...]` in the TEXT CHANGES section are VML connector/arrow position metadata,
  NOT embedded images. If such a line changed, report it as "Procedure changed" or
  "Editorial/Structural" — NEVER as "Visual change". The "Visual change" type is reserved
  exclusively for actual images shown in the "--- VISUAL CHANGES ---" section.

EQUIVALENCES — never create rows for swaps within:
  Obligation synonyms: shall = must = will = are to be = are required to
  Permission synonyms: may = can = could = is permitted to
  DO report obligation <-> permission crossings (type: Modal change, High).

SKIP entirely:
  - Pure formatting: whitespace, capitalisation, punctuation with no meaning change
  - Obligation synonym swaps (shall / must / will)
  - Permission synonym swaps (may / can / could)
  - Document metadata ONLY: revision history tables, approval/signature blocks, document
    issue dates, page numbers, headers/footers with no technical content
  - Reference formatting only: NAS 410 vs NAS410 = same document, same revision
"""

_HEADER = """\
You are a technical change impact analyst. Output ONLY valid JSON — no prose, no markdown, no code fences.

OUTPUT FORMAT — a JSON array, one object per atomic change:

[
  {{
    "section": "<section title from document>",
    "page": "<number from [Page N] or [Slide N] annotation in the diff, e.g. \\"3\\" or \\"3→5\\" for MODIFIED (old→new); null if unavailable>",
    "type": "<TYPE — see list below>",
    "criticality": "High|Medium|Low",
    "before": "{before_desc}",
    "after":  "{after_desc}",
    "rationale": "{rationale_desc}"
  }}
]
"""

_FOOTER = """\
If nothing qualifies: output exactly []

IMPORTANT: output ONLY the JSON array. No explanation. No markdown. No code fences.\
"""


def _assemble(before_desc: str, after_desc: str, rationale_desc: str, quality_section: str) -> str:
    """Glue the common sections around a variant's JSON-schema field descriptions and QUALITY rules."""
    return (
        _HEADER.format(
            before_desc=before_desc,
            after_desc=after_desc,
            rationale_desc=rationale_desc,
        )
        + '\n'
        + _TYPES_AND_CRITICALITY
        + '\n'
        + _DIFF_AND_RULES
        + '\n'
        + quality_section
        + '\n'
        + _FOOTER
    )


# ---------------------------------------------------------------------------
# BASELINE — the current production prompt (matches SYSTEM_PROMPT_STRUCTURED)
# ---------------------------------------------------------------------------

_BASELINE_QUALITY = """\
BEFORE / AFTER QUALITY — every row must satisfy these rules:
  • Quote exact wording from the diff whenever the text fits (max 40 words).
  • For VALUE CHANGES: always include the exact number and its unit.
      Bad:  before: \"old torque value\"            Good: before: \"torque 35 ± 2 N·m\"
  • For REFERENCE CHANGES: include the full document ID and revision.
      Bad:  before: \"old standard\"                Good: before: \"NAS 410 Rev 3\"
  • For REQUIREMENTS: quote the obligation keyword (shall/must/may) and the full clause.
      Bad:  before: \"must do X\"
      Good: before: \"the operator shall verify connector torque at each installation step\"
  • NEVER write meta-phrases: \"the document states\", \"old version says\", \"text reads\", etc.
  • \"--\" means entirely absent in that version — do NOT use \"--\" for partial changes.

RATIONALE QUALITY — every rationale must be actionable:
  • Name the specific system, component, part number, or process affected.
  • State what a technician or engineer must do differently as a result.
      Bad:  \"The requirement was modified.\"
      Bad:  \"This change affects the document.\"
      Good: \"Torque limit reduction on J3 connector requires re-qualification of harness install per updated IPC drawing.\"
      Good: \"New mandatory post-test inspection step added to acceptance procedure 7053-05 — production sign-off criteria change.\"
"""

STRUCTURED_PROMPT_BASELINE = _assemble(
    before_desc=(
        '<exact old text — quote verbatim when possible (max 40 words); '
        'for values always include the unit; use -- if newly added>'
    ),
    after_desc=(
        '<exact new text — quote verbatim when possible (max 40 words); '
        'for values always include the unit; use -- if removed>'
    ),
    rationale_desc=(
        '<one specific sentence: name the affected component/system/process '
        'and state the technical consequence — what must change in practice '
        '(assembly, test, compliance, airworthiness)>'
    ),
    quality_section=_BASELINE_QUALITY,
)


# ---------------------------------------------------------------------------
# VARIANT A — Strict word budgets per criticality
# ---------------------------------------------------------------------------

_A_QUALITY = """\
BEFORE / AFTER LENGTH BUDGETS — strict word caps by criticality:

  High criticality   → max 40 words. Verbatim quote of the changed clause with
                       its full unit, value, or obligation. Skip surrounding
                       context that didn't change.
  Medium criticality → max 20 words. Verbatim quote of the changed phrase only.
                       Drop the rest of the sentence.
  Low criticality    → max 8 words. The smallest fragment that identifies the
                       change — usually a label, value, section name, or
                       reference code.

  Rules that apply to all three:
    • Numeric values ALWAYS include unit (\"35 N·m\", \"25 °C\").
    • References ALWAYS include revision (\"NAS 410 Rev 3\").
    • Never write meta-phrases (\"the document states\", \"old version says\").
    • \"--\" only when content is entirely absent in that version.

RATIONALE LENGTH BUDGETS — strict caps, may be empty:

  High criticality   → 1 sentence (≤ 20 words). Name the affected component
                       and the action a technician/engineer must take.
                       Example: \"Re-torque J3 connectors at next maintenance.\"
  Medium criticality → ≤ 12 words. Name the affected area and consequence.
                       Example: \"Test method updated — verification step added.\"
  Low criticality    → \"\" (empty string). Low-criticality changes are
                       editorial / cosmetic; the before/after columns explain
                       themselves and a rationale would add noise.
"""

STRUCTURED_PROMPT_A = _assemble(
    before_desc=(
        '<old text — verbatim quote; word cap by criticality (High≤40, '
        'Medium≤20, Low≤8); always include units for values; use -- if newly added>'
    ),
    after_desc=(
        '<new text — verbatim quote; word cap by criticality (High≤40, '
        'Medium≤20, Low≤8); always include units for values; use -- if removed>'
    ),
    rationale_desc=(
        '<actionable consequence; word cap by criticality (High≤20, Medium≤12, '
        'Low="" empty string)>'
    ),
    quality_section=_A_QUALITY,
)


# ---------------------------------------------------------------------------
# VARIANT B — Information density (minimum necessary, no fixed budget)
# ---------------------------------------------------------------------------

_B_QUALITY = """\
BEFORE / AFTER — minimum necessary information:

  The reader scans dozens of rows. Cut every word that does not contribute to
  describing the change. Aim for the SHORTEST text that still conveys what
  was there vs. what is there now.

  Apply this principle for every type of change:
    • Numeric values  → number + unit, nothing else.
        Good: \"35 N·m\"        Bad: \"the torque value was set at 35 N·m\"
    • Text rewrites   → quote ONLY the words that changed, never the unchanged
                       surrounding sentence.
        Good: \"verify\"       Bad: \"the operator shall verify the connector torque\"
                                (if only \"check\" became \"verify\")
    • New / removed clauses → verbatim quote if ≤ 25 words, otherwise summarise
                       to the essential meaning.
    • References      → document ID + revision only.
        Good: \"NAS 410 Rev 3\"  Bad: \"the normative document NAS 410, revision 3\"

  Hard ceiling: 30 words. Aim well below for most rows. Never write
  meta-phrases (\"the document\", \"old version\", \"text\").
  \"--\" only when content is entirely absent.

RATIONALE — write only when the change is NOT self-evident:

  Skip rationale (output \"\") when:
    • The before/after pair already explains itself in plain technical terms.
    • The change is editorial, cosmetic, renumbering, or TOC.
    • Reading the before/after pair tells the reader what to do.

  Write a rationale (one sentence, ≤ 25 words) only when an outside reader
  would NOT understand the operational implication from before/after alone.

  Never repeat what the before/after columns already say. Explain ONLY why
  the change matters, and only when that is not obvious.
"""

STRUCTURED_PROMPT_B = _assemble(
    before_desc=(
        '<minimum text that conveys what was there — quote only the changed '
        'words, not surrounding sentence; numbers with unit only; use -- if new>'
    ),
    after_desc=(
        '<minimum text that conveys what is there now — quote only the changed '
        'words, not surrounding sentence; numbers with unit only; use -- if removed>'
    ),
    rationale_desc=(
        '<one short sentence ONLY when the before/after pair is not self-evident; '
        'empty string "" otherwise>'
    ),
    quality_section=_B_QUALITY,
)


# ---------------------------------------------------------------------------
# VARIANT C — Outcome / action driven
# ---------------------------------------------------------------------------

_C_QUALITY = """\
BEFORE / AFTER — substance only, no narration:

  Quote ONLY the substance of the change. The section column already says
  where the change lives, so do not repeat the section in the before/after
  field. The type and criticality columns already say what kind of change it
  is, so do not narrate that either.

  • Numeric: number + unit, nothing else.
      Good: \"35 N·m\"         Bad: \"the limit was 35 N·m, see para 4.3.1\"
  • Requirement: keep the obligation verb + the changed words.
      Good: \"shall verify torque at each step\"
      Bad:  \"the operator shall verify the connector torque at each installation step per the manual\"
  • Reference: \"NAS 410 Rev 3\". Never \"the document NAS 410 Rev 3, cited in para 2.1\".
  • No introductory text. No \"see also\". No section repetition.

  Hard ceiling: 25 words. Aim for half of that.
  Never write meta-phrases. \"--\" only when content is entirely absent.

RATIONALE — answer \"what concrete action does this trigger?\" or stay empty:

  Required ONLY when the change forces a downstream action:
    • Update a drawing, manual, or routing card
    • Re-qualify a procedure or harness install
    • Re-torque / re-inspect a component
    • Run a new test or skip an existing one
    • Update training, signage, or part-supply list
    • Change an acceptance criterion or sign-off step

  If no concrete downstream action is triggered (editorial, cosmetic,
  renumbering, TOC), set rationale to \"\" (empty string).

  Format: lead with the noun (component, drawing, procedure), then the action.
    Good: \"Re-torque J3 connectors at next scheduled maintenance.\"
    Good: \"Update installation drawing 7053-06 to reflect P2 removal.\"
    Bad:  \"This change requires technicians to re-torque the J3 connectors...\"
    Bad:  \"The document was modified to reflect the new procedure.\"

  Hard ceiling: 25 words.  Never explain what changed — that is the
  before/after.  Explain only what to do.
"""

STRUCTURED_PROMPT_C = _assemble(
    before_desc=(
        '<substance of the change only — number+unit, or obligation verb+changed words; '
        'no surrounding context; max 25 words; use -- if new>'
    ),
    after_desc=(
        '<substance of the change only — number+unit, or obligation verb+changed words; '
        'no surrounding context; max 25 words; use -- if removed>'
    ),
    rationale_desc=(
        '<concrete downstream action (max 25 words, noun-first); empty string "" '
        'when the change forces no action (editorial / renumbering / cosmetic)>'
    ),
    quality_section=_C_QUALITY,
)


# ---------------------------------------------------------------------------
# VARIANT D — Variant C + explicit "minor wording" suppression
# ---------------------------------------------------------------------------
#
# Identical to C (the current production prompt) except it closes the two
# noise leaks observed on the NAS 410 Ed.6→Ed.7 sample:
#   1. Number RENDERING changes (digits vs spelled-out, SAME value) were being
#      reported as "Value changed / High" — e.g. "24 months" -> "twenty-four
#      (24) months". These are house-style, not value changes.
#   2. Document issue/revision dates still slipped through despite the SKIP rule.
# It also reinforces the synonym/word-order skip with concrete examples.
#
# The extra guidance is appended to C's QUALITY section AND injected as a new
# "NUMERIC RENDERING" bullet in the EQUIVALENCES list of _DIFF_AND_RULES, but
# since _DIFF_AND_RULES is shared, we instead fold the rule into the QUALITY
# text (the only per-variant slot) to keep the taxonomy identical across variants.

_D_QUALITY = _C_QUALITY + """\

SAME-VALUE / SAME-MEANING — these are NOT changes; SKIP entirely (output no row):
  • NUMERIC RENDERING: a number written in digits vs spelled out is the SAME
    value when magnitude and unit are unchanged.
      SKIP: "240 hours" vs "two hundred forty (240) hours"
      SKIP: "24 months" vs "twenty-four (24) months"
      SKIP: "up to 2 years" vs "up to two (2) years"
    Report a "Value changed" row ONLY when the magnitude or unit actually
    differs (e.g. 240 → 200 hours, or hours → days). Never on rendering alone.
  • PUNCTUATION IN NUMBERS: "3-4 year" vs "3–4 year" (hyphen vs en-dash) — SKIP.
  • DOCUMENT DATES: issue date, revision date, "REVISION DATE: ..." — SKIP, even
    when the date value changed. It is metadata, not technical content.
  • SYNONYM / WORD-ORDER: "this standard" vs "this document",
    "practical and specific" vs "specific and practical" — SKIP when meaning is
    unchanged.
"""

STRUCTURED_PROMPT_D = _assemble(
    before_desc=(
        '<substance of the change only — number+unit, or obligation verb+changed words; '
        'no surrounding context; max 25 words; use -- if new>'
    ),
    after_desc=(
        '<substance of the change only — number+unit, or obligation verb+changed words; '
        'no surrounding context; max 25 words; use -- if removed>'
    ),
    rationale_desc=(
        '<concrete downstream action (max 25 words, noun-first); empty string "" '
        'when the change forces no action (editorial / renumbering / cosmetic)>'
    ),
    quality_section=_D_QUALITY,
)


# ---------------------------------------------------------------------------
# Registry / accessor
# ---------------------------------------------------------------------------

VARIANTS = {
    'baseline': STRUCTURED_PROMPT_BASELINE,
    'A':        STRUCTURED_PROMPT_A,
    'B':        STRUCTURED_PROMPT_B,
    'C':        STRUCTURED_PROMPT_C,
    'D':        STRUCTURED_PROMPT_D,
}


def get_variant(name: str) -> str:
    """Return the prompt string for the named variant.

    Names (case-insensitive): 'baseline', 'A', 'B', 'C'.
    """
    if not isinstance(name, str):
        raise TypeError(f'variant name must be a string, got {type(name).__name__}')
    norm = name.strip().lower()
    lookup = {k.lower(): v for k, v in VARIANTS.items()}
    if norm not in lookup:
        valid = ', '.join(VARIANTS.keys())
        raise KeyError(f'Unknown variant {name!r}. Valid names: {valid}')
    return lookup[norm]
