# Databricks notebook source
# MAGIC %md
# MAGIC # Code patterns
# MAGIC
# MAGIC Gives every code of the inventory exactly one pattern, and the pattern gives its treatment for keyword search.
# MAGIC
# MAGIC The keyword side of the hybrid search splits `SF-1027` into `SF` and `1027`, two common words: the one passage that cites the code does not come up. The **canonical token** `SF1027` is one rare word, appended next to the code in the passage and in the question, so both share it.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | `dev_proj.qualibot.f1_inventaire_codes` (code inventory), `<env>_proj.qualibot.doc_catalog` for each environment of `ENVS` |
# MAGIC | Writes | `dev_proj.qualibot.f2_familles_codes` (one row per code and environment) and `dev_proj.qualibot.f3_cles_codes` (the dictionary of match keys), both rewritten |
# MAGIC | Rights | the user's own (read on DEV and UAT, write on the DEV analysis table) |
# MAGIC
# MAGIC ## Treatments
# MAGIC
# MAGIC | Treatment | Effect on the indexed text | Example |
# MAGIC | --- | --- | --- |
# MAGIC | `CANONICAL_TOKEN` | canonical token appended after the code, original text unchanged; same on passages and questions | `SF-1027` → `SF-1027 [SF1027]` |
# MAGIC | `KEEP_AS_IS` | text indexed unchanged: already one token, or not a code | `LEVEL 2`, `NAS1149C0332R` |
# MAGIC | `GLOSSARY_CANDIDATE` | letters-only acronym, unchanged; input for the acronym glossary | `DOA`, `EASA` |
# MAGIC
# MAGIC **Safety rule.** `KEEP_AS_IS` leaves the text exactly as it is indexed today: a code put there by mistake is never found worse than today, only not improved. Every letters-and-digits token that no exclusion pattern matches gets `CANONICAL_TOKEN` (`OTH-01`). So the code patterns are wide (they only name what they capture) and the exclusion patterns are narrow.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Vocabulary
# MAGIC
# MAGIC | Term | Meaning | Example |
# MAGIC | --- | --- | --- |
# MAGIC | spelling | the code as written in a document or a question | `SF-1027`, `sf 1027` |
# MAGIC | key | spelling in upper case, letters and digits only; one row of the table per key | `SF1027` |
# MAGIC | piece | a spelling is cut before each separator (`-` `_` `/` `.` space) and between a digit and the letter after it | `MI-13991Table 3` → `MI`, `-13991`, `Table`, ` 3` |
# MAGIC | slice | one or more consecutive pieces | `MI-13991`, `13991Table` |
# MAGIC | signature | leading letters kept, then `9` per digit and `A` per letter | `Q0375MI` → `Q9999AA`, `SF1027` → `SF9999` |
# MAGIC | catalogue key | key of a catalogue REF, with and without its language suffix (`FR` `EN` `GB` `MX` `BG` `CZ` `BR` `ES`) | `MI-1413_FR` → `MI1413FR` and `MI1413` |
# MAGIC | canonical token | for a REF, its catalogue key without language suffix; otherwise the key | `Q0697MI_FR` → `Q0697MI` |
# MAGIC
# MAGIC A code is linked to a catalogue REF when its key is a catalogue key, the key of the REF without its first word when the rest is letters, separator, digits (`IT-243` for `LdB IT-243`), the key of the REF without its final letters (`QIS-46A-352` for `QIS-46A-352 A`), or when one slice of a spelling is a catalogue key (`MI-13991` in `MI-13991Table 3`, `QP-2086` in `QP-2086-Software`). The longest such slice wins. That slice is the **match key**: the part of the text the matcher recognises and follows with the token.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Intraqual REF formats
# MAGIC
# MAGIC A code linked to a catalogue REF takes the format of that REF. Formats are tested on the REF in upper case, in this order; a REF that matches none is `REF-99`. Detection uses the catalogue itself, so a REF with a format absent from this table is still found: the regexes only describe the formats. REF counts are computed by the cell *REF formats in the catalogue*.
# MAGIC
# MAGIC | ID | Format | Regex (catalogue REF, upper case) | Prefixes | Catalogue examples | Spellings found in text | Canonical token |
# MAGIC | --- | --- | --- | --- | --- | --- | --- |
# MAGIC | `REF-01` | Prefix-number | `^[A-Z]+-[0-9]+$` | MI, NF, GO, MR, MIT, QP, FORM, NS, DGL, FDAQL, MM, IC, IP, INAQ, MOP, DLV, SF, QM | `MI-13803`, `QP-2080`, `SF-1029` | `MI 1058`, `MI -1058`, `MI - 1365`, `NF_10845` | `MI13803` |
# MAGIC | `REF-02` | Prefix-number with language | `^[A-Z]+-[0-9]+[_ -](FR\|EN\|GB\|MX\|BG\|CZ\|BR\|ES\|ESP)$` | as REF-01 | `MI-13826_GB`, `FORM-566 MX`, `MI-1085-EN` | `MI-1100_GB`, `NF-10149_Fr`, `MM1059GB` | `MI13826` |
# MAGIC | `REF-03` | Site code with document type | `^[A-Z][0-9]{4}[A-Z]{1,2}(_[A-Z]{2})?$` | Q, P, E, H, L, F; type QP, QR, MI, GO, MM, NF, F | `Q0009QP`, `P0323F`, `Q0674MI_GB` | `Q0697MI_FR`, `Q0054QR-E`, `Q0447MI/B` | `Q0009QP` |
# MAGIC | `REF-04` | Lettre de branchement | `^LDB [A-Z]{1,2}-[0-9]{3}$` | LdB IT, P, CI, F, NF | `LdB IT-243`, `LdB P-155` | `LDB IT 243`, `LdB-P099`, `IT-243` | `LDBIT243` |
# MAGIC | `REF-05` | Segmented procedure | `^[A-Z]+[0-9]?-[0-9]{2}-[0-9]+$` | DLV, QTP, QTR, TDAJ, TDA, UDA, UDB, UDC, UDE, R | `DLV-31-5173`, `QTP-01-245`, `UDE2-00-113` | `QTP 01-174`, `DLV31-5117`, `TDA8 - 00/027` | `DLV315173` |
# MAGIC | `REF-06` | QIS | `^QIS-[0-9]{2}[A-Z]-[0-9]{3}( [A-Z])?$` | QIS | `QIS-46A-352 A`, `QIS-45D-208` | `QIS-46A-352`, `QIS-46B-035-a` | `QIS46A352A` |
# MAGIC | `REF-07` | IF instruction | `^IF2[0-9]{4}(_[A-Z]{2})?$` | IF | `IF20322_BR`, `IF20168` | `IF2.03.08_CZ`, `IF 2.03.16`, `IF 20316` | `IF20322` |
# MAGIC | `REF-08` | Glued internal reference | `^[A-Z]{2,5}[0-9]{3,4}(-[0-9])?([_.][A-Z]{2}){0,2}$` | PRLAT, INAQ, INE, FDAQL | `PRLAT566_FR`, `INE028_FR_EN` | `PR/LAT/506`, `IN/AQ/561`, `FD_AQL_2193`, `PRLAT528.FR` | `PRLAT566` |
# MAGIC | `REF-09` | IQ register | `^IQ[-_]?[0-9]{2}[-_][0-9]{3,5}(_[A-Z][0-9]{2})?(_[A-Z]{2})?$` | IQ | `IQ20_384_EN`, `IQ_20_256_EN`, `IQ14_513_R30_FR` | `IQ 26-1450`, `IQ19-807` | `IQ20384` |
# MAGIC | `REF-10` | Programme phase | `^(PRO\|PROLAT\|OBS)[-_][A-Z][0-9][0-9A-Z]?([-_][A-Z]+)?$` | PRO, PROLAT, OBS | `PRO-R80`, `PROLAT_R40_FR`, `OBS_A8P` | `PRO R80` | `PROLATR40` |
# MAGIC | `REF-11` | Spaced segments | `^[A-Z]{3}( [0-9]{2}){2,3}$` | IAQ, RAQ, PAQ | `IAQ 04 04 05`, `PAQ 09 21` | `IAQ04 04 05`, `PAQ 10.03` | `IAQ040405` |
# MAGIC | `REF-12` | Numeric | `^[0-9]{2,3}([- ]+-?[0-9]+)+$` | none | `115-000-00-108`, `19 --058` | `190.62`, `19058` | `11500000108` |
# MAGIC | `REF-99` | Other catalogue REF | none: exact catalogue key | ŘLCZ, 777F, L, A1O, IN, MQP, LMX, DAP, … | `ŘLCZ-07/18`, `777F-MDCD-QP`, `L_1311_CMP` | as for the others | key of the REF |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Other codes
# MAGIC
# MAGIC Codes that are not linked to a catalogue REF. *Key* regexes apply to the key (upper case, separators removed), *spelling* regexes to every spelling as written.
# MAGIC
# MAGIC | ID | Pattern | Regex | Captures | Examples | Treatment |
# MAGIC | --- | --- | --- | --- | --- | --- |
# MAGIC | `ACR-02` | Separated acronym | spelling, case-sensitive: `^[A-Z]{1,3}([./][A-Z]{1,3})+\.?$` | acronyms written with dots or slashes | `E.A.S.A`, `B.P.`, `P/N`, `N.D.T` | `CANONICAL_TOKEN` (`[EASA]`) |
# MAGIC | `ACR-01` | Acronym | key: `^[A-Z]+$` | letters-only words in upper case: acronyms and upper-case common words | `DOA`, `EASA`, `NADCAP`, `LATECOERE` | `GLOSSARY_CANDIDATE` |
# MAGIC | `UNL-01` | Unlisted REF | a slice, language suffix removed, has the signature of a catalogue REF (leading letters, then at least one digit) | REFs cited but absent from the catalogue: obsolete, other site, typo | `SF-1027` (as `SF-1029`), `NS-1717`, `MI-13663`, `PAQ 10 03` | `CANONICAL_TOKEN` |
# MAGIC | `MAT-01` | Material or alloy | key: `^([0-9]{4}T[0-9]+$\|[0-9]{3}PH$\|TA[0-9]+V\|TI[0-9]\|[XZ][0-9]+C[RN])` | aluminium tempers, steels, titanium alloys | `2024-T3`, `7175-T7351`, `15-5PH`, `TA6V`, `Ti-6Al-4V`, `Z6CNU17-04` | `CANONICAL_TOKEN` |
# MAGIC | `STD-01` | Standard or specification | key: `^(EN\|PREN\|ISO\|AS\|NAS\|NSA\|MS\|ABS\|ASNA\|BAC\|BACB\|BACS\|BSS\|BAPS\|BMS\|HJPS\|NE\|NF\|AIPI\|AIPS\|AITM\|IPDA\|DO\|ED\|ARP\|AMS\|ASTM\|MIL\|DIN\|SAE\|IPC\|IEC\|LN\|HST\|HL\|CCR)[0-9]` | aerospace, Airbus, Boeing, Dassault standards and part standards | `EN 9100`, `AS9100`, `BAC 5578`, `NE 40-012`, `AIPI 05-02-011`, `ED-79A` | `CANONICAL_TOKEN` |
# MAGIC | `REG-01` | Regulation | key: `^(PART\|PARTIE\|CS\|AMC)[0-9]` or `^21[GJ]$` | EASA parts, certification specifications, Part 21 subparts | `PART 145`, `Part-21`, `CS 25-1309`, `AMC1 21`, `21-J`, `Partie-145.A.90` | `CANONICAL_TOKEN` |
# MAGIC | `ACF-01` | Aircraft or programme | key: `^(A[0-9]{3}M?\|B7[0-9]7\|BOEING7[0-9]7\|F[0-9]X\|ATR[0-9]{2}([0-9]{2})?\|ERJ[0-9]{3})$` | aircraft families | `A320`, `A 350`, `A400M`, `B 787`, `F7X`, `ATR 42/72` | `CANONICAL_TOKEN` |
# MAGIC | `SHC-01` | Short code | key: `^[A-Z]{1,2}[0-9]{1,2}$` | licence categories, phases, zones, chapters | `B1`, `B 2`, `R-30`, `D1`, `CH15`, `ZP09` | `CANONICAL_TOKEN` |
# MAGIC | `IDN-01` | Segmented identifier | spelling: `^[A-Z][A-Za-z0-9]*([-_/. ]{1,3}[A-Za-z0-9]+)+$` with 3 consecutive digits | internal numbers, tools, lots, drawings | `STM-0002396`, `HAR-998`, `MEP 10-069`, `IN.ME.L269` | `CANONICAL_TOKEN` |
# MAGIC | `GLU-01` | Glued code | no spelling contains a separator | part and serial numbers written as one word | `NAS1149C0332R`, `IS0024879M`, `S02P02` | `KEEP_AS_IS` (already one token) |
# MAGIC | `OTH-01` | Other code | letters and digits matched by no other pattern | long tail: labels, tool references, mixed spellings | `C3-Fill`, `tool_8660197`, `BOE_BACC65CR3PN` | `CANONICAL_TOKEN` |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Excluded tokens
# MAGIC
# MAGIC Narrow patterns for tokens that are not codes. They all keep `KEEP_AS_IS`.
# MAGIC
# MAGIC | ID | Pattern | Regex | Source in documents | Examples |
# MAGIC | --- | --- | --- | --- | --- |
# MAGIC | `NUM-01` | Number only | key: `^[0-9]+$` | section numbers, decimals, dates | `4.2.1`, `6.1`, `24032022` |
# MAGIC | `EXC-09` | URL, path or file name | one spelling: `^https?`, `^www\.`, `\.(aspx?\|local\|corp\|com\|net\|org\|html?\|pdf\|docx?\|xlsx?\|pptx?\|png\|jpe?g\|txt\|csv)($\|[/.?#])`, or a lower-case `/path/` | links, file references | `prtouintraqual1/intraqual/identification.aspx`, `20LBG_B.pdf` |
# MAGIC | `EXC-01` | Table column label | key: `^(COL\|OA\|OC)[0-9]+$` | spreadsheet headers | `COL6`, `OA21`, `OC20` |
# MAGIC | `EXC-02` | Heading number glued to a word | key: `^[0-9]+[A-Z]{3,}` | tables of contents | `2SCOPE4`, `1OBJECT4`, `11REFERENCE` |
# MAGIC | `EXC-03` | Measure unit or ordinal | key: `^[0-9]+(MM\|CM\|…\|ST\|ND\|RD\|TH\|ER\|EME\|E)$`, units of `UNITS` | quantities, ordinals | `5MM`, `60KHZ`, `3D`, `1ST`, `2ND` |
# MAGIC | `EXC-04` | Dimension | key: `^[0-9]+(X[0-9]+)+$` | sizes | `125x80`, `76X254X1` |
# MAGIC | `EXC-05` | List item | every spelling: `^[0-9]{1,3}[-_/. )]?[A-Za-z]{1,2}$` | numbered list bullets | `3.P`, `1-b`, `10P` |
# MAGIC | `EXC-06` | Date with month name | one spelling: month (French or English, abbreviated or not), up to 3 separators, a digit | dates | `July 2024`, `Jan/31/2024`, `JANVIER2018` |
# MAGIC | `EXC-07` | Word and small number | key `^[A-Z]{3,}[0-9]{1,2}$`, or every spelling `^[A-Za-z]{3,}[-_/. ]{0,3}[0-9]{1,2}([-_/. ]{1,3}[0-9]{1,2})*$`; the word is not the leading letters of a catalogue REF | levels, steps, figures, tables | `LEVEL 2`, `NIVEAU 3`, `TABLE 1/1`, `Figure 13` |
# MAGIC | `EXC-08` | Word and number, never glued | every spelling: `^[A-Za-z]+ [0-9]+$` | running text | `protection 999`, `inspect 100`, `rotated 180` |
# MAGIC
# MAGIC ## Evaluation order
# MAGIC
# MAGIC First match wins: `REF-01` … `REF-99`, `ACR-02`, `ACR-01`, `NUM-01`, the Part 21 subparts `21G` and `21J` (`REG-01`), `UNL-01`, `EXC-09`, `EXC-01` … `EXC-04`, `MAT-01`, `EXC-05`, `EXC-06`, `STD-01`, `REG-01`, `ACF-01`, `SHC-01`, `EXC-07`, `IDN-01`, `EXC-08`, `GLU-01`, `OTH-01`. The Part 21 subparts come before the units and list items so that `21G` and `21J` stay codes, materials before the list items so that `15-5PH` stays one; catalogue REFs and unlisted REFs come before every exclusion.
# MAGIC
# MAGIC ## Output table `dev_proj.qualibot.f2_familles_codes`
# MAGIC
# MAGIC | Column | Type | Content |
# MAGIC | --- | --- | --- |
# MAGIC | `env` | string | `dev` or `uat` |
# MAGIC | `code_key` | string | key: `SF1027` |
# MAGIC | `spellings` | array&lt;string&gt; | spellings seen in documents and questions: `SF-1027`, `SF 1027` |
# MAGIC | `pattern_id`, `pattern_name`, `pattern_regex` | string | pattern of the tables above |
# MAGIC | `treatment` | string | `CANONICAL_TOKEN`, `KEEP_AS_IS`, `GLOSSARY_CANDIDATE` |
# MAGIC | `canonical_token` | string | token appended to the text; null unless `CANONICAL_TOKEN` |
# MAGIC | `match_key` | string | key the matcher recognises in the text: the key, or the slice linked to a REF (`MI13991` for `MI-13991Table 3`); null unless `CANONICAL_TOKEN` |
# MAGIC | `catalogue_ref` | string | catalogue REF of the code; for `UNL-01`, a REF with the same signature |
# MAGIC | `intraqual_url` | string | Intraqual link of `catalogue_ref` |
# MAGIC | `document_count` | long | parsed documents citing the code |
# MAGIC | `question_count` | long | chat questions citing the code |
# MAGIC | `top_documents` | array&lt;string&gt; | the 10 document REFs citing it most, `REF (n)` |
# MAGIC
# MAGIC ## Dictionary `dev_proj.qualibot.f3_cles_codes`
# MAGIC
# MAGIC The matcher appends a token only for a key of this dictionary. One row per match key, for DEV and UAT together:
# MAGIC
# MAGIC | Column | Content | Rows from |
# MAGIC | --- | --- | --- |
# MAGIC | `match_key` | key recognised in the text | every catalogue key of every environment (cited or not, so that a question finds it); the `match_key` and the `canonical_token` of every `CANONICAL_TOKEN` code |
# MAGIC | `canonical_token` | token appended after it | catalogue REF without language suffix for a REF; `canonical_token` of the code otherwise |
# MAGIC | `pattern_id` | pattern of the code | format of the REF, or `pattern_id` of the code |
# MAGIC
# MAGIC A key found both ways keeps the catalogue row; between two codes, the one cited by more documents wins.

# COMMAND ----------

# Parameters

import logging
import sys

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F

ENVS = ["dev", "uat"]   # ["dev", "uat"], ["dev"] or ["uat"]
SOURCE = "dev_proj.qualibot.f1_inventaire_codes"
TARGET = "dev_proj.qualibot.f2_familles_codes"
DICTIONARY = "dev_proj.qualibot.f3_cles_codes"
INTRAQUAL_BASE = "https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref="

logger = logging.getLogger("code_patterns")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# COMMAND ----------

# Pattern catalogue: the regexes of the tables above, used as is by the classification

LANGUAGES = ["FR", "EN", "GB", "MX", "BG", "CZ", "BR", "ES"]

REF_FORMATS = [
    ("REF-01", "Prefix-number", r"^[A-Z]+-[0-9]+$"),
    ("REF-02", "Prefix-number with language", r"^[A-Z]+-[0-9]+[_ -](FR|EN|GB|MX|BG|CZ|BR|ES|ESP)$"),
    ("REF-03", "Site code with document type", r"^[A-Z][0-9]{4}[A-Z]{1,2}(_[A-Z]{2})?$"),
    ("REF-04", "Lettre de branchement", r"^LDB [A-Z]{1,2}-[0-9]{3}$"),
    ("REF-05", "Segmented procedure", r"^[A-Z]+[0-9]?-[0-9]{2}-[0-9]+$"),
    ("REF-06", "QIS", r"^QIS-[0-9]{2}[A-Z]-[0-9]{3}( [A-Z])?$"),
    ("REF-07", "IF instruction", r"^IF2[0-9]{4}(_[A-Z]{2})?$"),
    ("REF-08", "Glued internal reference", r"^[A-Z]{2,5}[0-9]{3,4}(-[0-9])?([_.][A-Z]{2}){0,2}$"),
    ("REF-09", "IQ register", r"^IQ[-_]?[0-9]{2}[-_][0-9]{3,5}(_[A-Z][0-9]{2})?(_[A-Z]{2})?$"),
    ("REF-10", "Programme phase", r"^(PRO|PROLAT|OBS)[-_][A-Z][0-9][0-9A-Z]?([-_][A-Z]+)?$"),
    ("REF-11", "Spaced segments", r"^[A-Z]{3}( [0-9]{2}){2,3}$"),
    ("REF-12", "Numeric", r"^[0-9]{2,3}([- ]+-?[0-9]+)+$"),
]
REF_OTHER = ("REF-99", "Other catalogue REF", "exact catalogue key")

STD_PREFIXES = ["EN", "PREN", "ISO", "AS", "NAS", "NSA", "MS", "ABS", "ASNA", "BAC", "BACB", "BACS", "BSS", "BAPS",
                "BMS", "HJPS", "NE", "NF", "AIPI", "AIPS", "AITM", "IPDA", "DO", "ED", "ARP", "AMS", "ASTM", "MIL",
                "DIN", "SAE", "IPC", "IEC", "LN", "HST", "HL", "CCR"]
UNITS = ["MM", "CM", "DM", "M", "KM", "UM", "ML", "CL", "L", "MG", "G", "KG", "N", "NM", "DAN", "KN", "BAR", "MBAR",
         "PSI", "PA", "KPA", "MPA", "HZ", "KHZ", "MHZ", "GHZ", "DB", "V", "KV", "MV", "A", "MA", "W", "KW", "H",
         "MIN", "MN", "S", "C", "K", "F", "IN", "FT", "LB", "LBS", "OZ", "PC", "PCS", "PT", "D", "X",
         "ST", "ND", "RD", "TH", "ER", "ERE", "EME", "E", "IEME", "O"]
PART21_SUBPARTS = ["21G", "21J"]
MONTHS = "JAN|JANV|FE[BV]|FEVR|MAR|MARS|APR|AVR|MAY|MAI|JUN|JUIN|JUL|JUIL|AUG|AOU|SEP|SEPT|OCT|NOV|DEC"

RX = {
    "ACR-02": r"^[A-Z]{1,3}([./][A-Z]{1,3})+\.?$",
    "ACR-01": r"^[A-Z]+$",
    "NUM-01": r"^[0-9]+$",
    "EXC-09": (r"(?i)(^https?|^www\.|\.(aspx?|local|corp|com|net|org|html?|pdf|docx?|xlsx?|pptx?|png|jpe?g|txt|csv)"
               r"($|[/.?#])|(?-i:/[a-z]{2,}/))"),
    "EXC-01": r"^(COL|OA|OC)[0-9]+$",
    "EXC-02": r"^[0-9]+[A-Z]{3,}",
    "EXC-03": f"^[0-9]+({'|'.join(sorted(set(UNITS), key=len, reverse=True))})$",
    "EXC-04": r"^[0-9]+(X[0-9]+)+$",
    "MAT-01": r"^([0-9]{4}T[0-9]+$|[0-9]{3}PH$|TA[0-9]+V|TI[0-9]|[XZ][0-9]+C[RN])",
    "EXC-05": r"^[0-9]{1,3}[-_/. )]?[A-Za-z]{1,2}$",
    "EXC-06": f"(?i)^({MONTHS})[a-z]*[ ./-]{{0,3}}[0-9]",
    "STD-01": f"^({'|'.join(STD_PREFIXES)})[0-9]",
    "REG-01": r"^(PART|PARTIE|CS|AMC)[0-9]|^21[GJ]$",
    "ACF-01": r"^(A[0-9]{3}M?|B7[0-9]7|BOEING7[0-9]7|F[0-9]X|ATR[0-9]{2}([0-9]{2})?|ERJ[0-9]{3})$",
    "SHC-01": r"^[A-Z]{1,2}[0-9]{1,2}$",
    "EXC-07": r"^[A-Z]{3,}[0-9]{1,2}$",
    "EXC-07-spelling": r"^[A-Za-z]{3,}[-_/. ]{0,3}[0-9]{1,2}([-_/. ]{1,3}[0-9]{1,2})*$",
    "IDN-01": r"^[A-Z][A-Za-z0-9]*([-_/. ]{1,3}[A-Za-z0-9]+)+$",
    "EXC-08": r"^[A-Za-z]+ [0-9]+$",
    "GLU-01": r"[-_/. ]",
}

PATTERNS = [
    *[(i, name, rx, "CANONICAL_TOKEN") for i, name, rx in REF_FORMATS],
    (*REF_OTHER, "CANONICAL_TOKEN"),
    ("ACR-02", "Separated acronym", f"spelling, case-sensitive: {RX['ACR-02']}", "CANONICAL_TOKEN"),
    ("ACR-01", "Acronym", RX["ACR-01"], "GLOSSARY_CANDIDATE"),
    ("NUM-01", "Number only", RX["NUM-01"], "KEEP_AS_IS"),
    ("UNL-01", "Unlisted REF", "signature of a catalogue REF: leading letters, 9 per digit, A per letter",
     "CANONICAL_TOKEN"),
    ("EXC-09", "URL, path or file name", f"one spelling: {RX['EXC-09']}", "KEEP_AS_IS"),
    ("EXC-01", "Table column label", RX["EXC-01"], "KEEP_AS_IS"),
    ("EXC-02", "Heading number glued to a word", RX["EXC-02"], "KEEP_AS_IS"),
    ("EXC-03", "Measure unit or ordinal", RX["EXC-03"], "KEEP_AS_IS"),
    ("EXC-04", "Dimension", RX["EXC-04"], "KEEP_AS_IS"),
    ("MAT-01", "Material or alloy", RX["MAT-01"], "CANONICAL_TOKEN"),
    ("EXC-05", "List item", f"every spelling: {RX['EXC-05']}", "KEEP_AS_IS"),
    ("EXC-06", "Date with month name", f"one spelling: {RX['EXC-06']}", "KEEP_AS_IS"),
    ("STD-01", "Standard or specification", RX["STD-01"], "CANONICAL_TOKEN"),
    ("REG-01", "Regulation", RX["REG-01"], "CANONICAL_TOKEN"),
    ("ACF-01", "Aircraft or programme", RX["ACF-01"], "CANONICAL_TOKEN"),
    ("SHC-01", "Short code", RX["SHC-01"], "CANONICAL_TOKEN"),
    ("EXC-07", "Word and small number",
     f"{RX['EXC-07']} or every spelling {RX['EXC-07-spelling']}, word not a REF prefix", "KEEP_AS_IS"),
    ("IDN-01", "Segmented identifier", f"one spelling: {RX['IDN-01']}, with 3 consecutive digits", "CANONICAL_TOKEN"),
    ("EXC-08", "Word and number, never glued", f"every spelling: {RX['EXC-08']}", "KEEP_AS_IS"),
    ("GLU-01", "Glued code", "no spelling contains a separator", "KEEP_AS_IS"),
    ("OTH-01", "Other code", "letters and digits, no other pattern", "CANONICAL_TOKEN"),
]
PATTERN_ORDER = [p[0] for p in PATTERNS]
logger.info("%d patterns", len(PATTERNS))

# COMMAND ----------

# Functions (no write)

SEPARATOR = "[-_/. ]"
# Cut before each separator and between a digit and the letter after it.
PIECE_CUT = "(?=[-_/. ])|(?<=[0-9])(?=[A-Za-z])"


def to_key(col: Column | str) -> Column:
    """Key of a string: upper case, letters and digits only.

    :param col: text column or column name
    :return: column of the key
    """
    return F.upper(F.regexp_replace(col, "[^A-Za-z0-9]", ""))


def without_language(col: Column | str) -> Column:
    """Key of a REF without its language suffix (``MI-1413_FR`` gives ``MI1413``).

    :param col: column of the REF
    :return: column of the key
    """
    return to_key(F.regexp_replace(F.trim(col), f"(?i)[-_. ]+({'|'.join(LANGUAGES)})$", ""))


def leading_cuts(col: Column | str) -> Column:
    """Starts of a string cut before each separator, the whole string excluded (``A-1-2`` gives ``A``, ``A-1``).

    :param col: text column
    :return: array column, empty when the string has no separator
    """
    parts = F.split(col, f"(?={SEPARATOR})")
    return F.when(F.size(parts) > 1,
                  F.transform(F.sequence(F.lit(1), F.size(parts) - 1),
                              lambda i: F.array_join(F.slice(parts, 1, i), ""))
                  ).otherwise(F.array().cast("array<string>"))


def slices(col: Column | str) -> Column:
    """Every run of consecutive pieces of a spelling, the whole spelling included.

    :param col: column of the spelling
    :return: array column of the slices (``PRO_Q0014NF_EN`` gives ``PRO``, ``PRO_Q0014``, ``Q0014NF``, …)
    """
    parts = F.split(col, PIECE_CUT)
    n = F.size(parts)
    return F.flatten(F.transform(
        F.sequence(F.lit(1), n),
        lambda i: F.transform(F.sequence(i, n), lambda j: F.array_join(F.slice(parts, i, j - i + 1), ""))))


def signature(name: str) -> Column:
    """Signature of a key: leading letters kept, then 9 per digit and A per letter (``Q0375MI`` gives ``Q9999AA``).

    :param name: name of the key column
    :return: column of the signature
    """
    head = f"regexp_extract({name}, '^([A-Z]*)', 1)"
    rest = f"substring({name}, length({head}) + 1)"
    return F.expr(f"concat({head}, regexp_replace(regexp_replace({rest}, '[0-9]', '9'), '[A-Z]', 'A'))")


def ref_format(col: Column | str) -> Column:
    """Format of a catalogue REF: the first of ``REF_FORMATS`` that matches it in upper case, else ``REF-99``.

    :param col: column of the REF as written in the catalogue
    :return: column of the pattern id
    """
    ref = F.upper(F.trim(col))
    out = F.lit(REF_OTHER[0])
    for pid, _, rx in reversed(REF_FORMATS):
        out = F.when(ref.rlike(rx), F.lit(pid)).otherwise(out)
    return out


def catalogue_keys(env: str) -> DataFrame:
    """Keys linked to the catalogue REFs of an environment, one REF per key.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns key, ref and rank (1: the REF key itself, 2: without first word,
        3: without final letters); the lowest rank wins
    """
    cat = spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL").distinct()
    is_code = F.col("key").rlike("[A-Z]") & F.col("key").rlike("[0-9]")
    whole = cat.select(to_key("ref").alias("key"), "ref")
    no_lang = cat.select(without_language("ref").alias("key"), "ref")
    rest = F.regexp_replace(F.trim("ref"), r"^\S+\s+", "")
    no_first_word = (cat.where(F.trim("ref").contains(" ") & rest.rlike("^[A-Za-z]+[-_/.][0-9]"))
                     .select(without_language(rest).alias("key"), "ref").where(is_code))
    no_final_letters = (cat.select("ref", F.explode(leading_cuts(F.trim("ref"))).alias("start"))
                        .where(to_key(F.expr("substring(trim(ref), length(start) + 1)")).rlike("^[A-Z]+$"))
                        .select(to_key("start").alias("key"), "ref").where(is_code))
    rows = (whole.unionByName(no_lang).withColumn("rank", F.lit(1))
            .unionByName(no_first_word.withColumn("rank", F.lit(2)))
            .unionByName(no_final_letters.withColumn("rank", F.lit(3))))
    w = Window.partitionBy("key").orderBy("rank", "ref")
    return rows.where(F.length("key") > 0).withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


def inventory(env: str) -> DataFrame:
    """One row per key of the documents and of the questions of an environment.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns code_key, spellings, document_count, question_count and top_documents
    """
    inv = spark.table(SOURCE).where((F.col("env") == env) & F.col("section").isin("B_corpus", "C_questions"))
    doc = F.col("section") == "B_corpus"
    return (inv.groupBy(F.col("cle").alias("code_key"))
            .agg(F.array_sort(F.array_distinct(F.flatten(F.collect_list("formes")))).alias("spellings"),
                 F.max(F.when(doc, F.col("docs"))).alias("document_count"),
                 F.max(F.when(~doc, F.col("docs"))).alias("question_count"),
                 F.max_by(F.col("refs_source"), F.when(doc, F.col("docs"))).alias("top_documents"))
            .select("code_key", "spellings",
                    F.coalesce("document_count", F.lit(0)).cast("long").alias("document_count"),
                    F.coalesce("question_count", F.lit(0)).cast("long").alias("question_count"),
                    "top_documents"))


def classify(env: str) -> DataFrame:
    """Pattern, treatment and canonical token of every key of an environment.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns of the output table
    """
    k = inventory(env)
    cat = catalogue_keys(env).withColumn("fmt", ref_format("ref")).cache()
    own_keys = cat.where("rank = 1").select(F.col("key").alias("piece_key"), F.col("ref").alias("slice_ref"),
                                            F.col("fmt").alias("slice_fmt"))
    signatures = (cat.where("rank = 1").select(signature("key").alias("sig"), "ref")
                  .where(F.col("sig").rlike("^[A-Z]+.*9"))
                  .groupBy("sig").agg(F.min("ref").alias("unl_ref")))
    pieces = (k.select("code_key", F.explode("spellings").alias("sp"))
              .select("code_key", F.explode(slices("sp")).alias("s"))
              .where(F.col("s").rlike("[A-Za-z]") & F.col("s").rlike("[0-9]"))
              .select("code_key", to_key("s").alias("piece_key"), without_language("s").alias("piece_nolang"))
              .distinct().cache())
    in_ref = (pieces.join(F.broadcast(own_keys), "piece_key")
              .groupBy("code_key").agg(F.max_by(F.struct("slice_ref", "slice_fmt", "piece_key"),
                                                F.length("piece_key")).alias("v"))
              .select("code_key", "v.slice_ref", "v.slice_fmt", F.col("v.piece_key").alias("slice_key")))
    unlisted = (pieces.select("code_key", "piece_nolang", signature("piece_nolang").alias("sig"))
                .join(F.broadcast(signatures), "sig")
                .groupBy("code_key").agg(F.max_by(F.struct("unl_ref", "piece_nolang"),
                                                  F.length("piece_nolang")).alias("u"))
                .select("code_key", "u.unl_ref", F.col("u.piece_nolang").alias("unl_key")))
    # Leading letters of the catalogue REFs: a word that is one of them is never a plain word (DLV-31).
    ref_prefixes = [r["p"] for r in cat.where("rank = 1")
                    .select(F.regexp_extract("key", "^([A-Z]+)", 1).alias("p")).where("p <> ''").distinct().collect()]

    key = F.col("code_key")
    prefix = F.regexp_extract(key, "^([A-Z]*)", 1)
    every = lambda rx: F.forall("spellings", lambda s: s.rlike(rx))   # noqa: E731
    some = lambda rx: F.exists("spellings", lambda s: s.rlike(rx))    # noqa: E731
    df = (k.join(cat.select(F.col("key").alias("code_key"), F.col("ref").alias("cat_ref"), "fmt"), "code_key", "left")
          .join(in_ref, "code_key", "left")
          .join(unlisted, "code_key", "left"))

    pattern = (F.when(F.col("fmt").isNotNull(), F.col("fmt"))
               .when(F.col("slice_fmt").isNotNull(), F.col("slice_fmt"))
               .when(some(RX["ACR-02"]) & key.rlike(RX["ACR-01"]), "ACR-02")
               .when(key.rlike(RX["ACR-01"]), "ACR-01")
               .when(key.rlike(RX["NUM-01"]), "NUM-01")
               .when(key.isin(PART21_SUBPARTS), "REG-01")
               .when(F.col("unl_ref").isNotNull(), "UNL-01")
               .when(some(RX["EXC-09"]), "EXC-09")
               .when(key.rlike(RX["EXC-01"]), "EXC-01")
               .when(key.rlike(RX["EXC-02"]), "EXC-02")
               .when(key.rlike(RX["EXC-03"]), "EXC-03")
               .when(key.rlike(RX["EXC-04"]), "EXC-04")
               .when(key.rlike(RX["MAT-01"]), "MAT-01")
               .when(every(RX["EXC-05"]), "EXC-05")
               .when(some(RX["EXC-06"]), "EXC-06")
               .when(key.rlike(RX["STD-01"]), "STD-01")
               .when(key.rlike(RX["REG-01"]), "REG-01")
               .when(key.rlike(RX["ACF-01"]), "ACF-01")
               .when(key.rlike(RX["SHC-01"]), "SHC-01")
               .when(~prefix.isin(ref_prefixes) & (key.rlike(RX["EXC-07"]) | every(RX["EXC-07-spelling"])), "EXC-07")
               .when(some(RX["IDN-01"]) & some("[0-9]{3}"), "IDN-01")
               .when(every(RX["EXC-08"]), "EXC-08")
               .when(~some(RX["GLU-01"]), "GLU-01")
               .otherwise("OTH-01"))
    catalogue_ref = (F.when(F.col("pattern_id").startswith("REF-"), F.coalesce("cat_ref", "slice_ref"))
                     .when(F.col("pattern_id") == "UNL-01", F.col("unl_ref")))
    is_ref = F.col("pattern_id").startswith("REF-")
    canonical = (F.when(F.col("treatment") != "CANONICAL_TOKEN", F.lit(None))
                 .when(is_ref, without_language("catalogue_ref"))
                 .when(F.col("pattern_id") == "UNL-01", F.col("unl_key"))
                 .otherwise(key))
    match_key = (F.when(F.col("treatment") != "CANONICAL_TOKEN", F.lit(None))
                 .when(is_ref & F.col("cat_ref").isNull(), F.col("slice_key"))
                 .when(F.col("pattern_id") == "UNL-01", F.col("unl_key"))
                 .otherwise(key))
    patterns = spark.createDataFrame(PATTERNS, "pattern_id string, pattern_name string, pattern_regex string, "
                                               "treatment string")
    return (df.withColumn("pattern_id", pattern)
            .join(F.broadcast(patterns), "pattern_id", "left")
            .withColumn("catalogue_ref", catalogue_ref)
            .withColumn("canonical_token", canonical)
            .withColumn("match_key", match_key)
            .select(F.lit(env).alias("env"), "code_key", "spellings", "pattern_id", "pattern_name", "pattern_regex",
                    "treatment", "canonical_token", "match_key", "catalogue_ref",
                    F.when(F.col("catalogue_ref").isNotNull(),
                           F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace("catalogue_ref", " ", "%20")))
                    .alias("intraqual_url"),
                    "document_count", "question_count", "top_documents"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build
# MAGIC Rewrites the pattern table with the environments of `ENVS`, then the dictionary.

# COMMAND ----------

out = None
for env in ENVS:
    part = classify(env)
    out = part if out is None else out.unionByName(part)
out.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET)
result = spark.table(TARGET).cache()
logger.info("written to %s: %d codes for %s", TARGET, result.count(), ENVS)

from_catalogue = None
for env in ENVS:
    part = catalogue_keys(env).select(F.col("key").alias("match_key"), without_language("ref").alias("canonical_token"),
                                      ref_format("ref").alias("pattern_id"), F.col("rank").alias("priority"),
                                      F.lit(None).cast("long").alias("document_count"))
    from_catalogue = part if from_catalogue is None else from_catalogue.unionByName(part)
coded = result.where(F.col("treatment") == "CANONICAL_TOKEN")
from_codes = (coded.select("match_key", "canonical_token", "pattern_id", "document_count")
              .unionByName(coded.select(F.col("canonical_token").alias("match_key"), "canonical_token", "pattern_id",
                                        "document_count"))
              .withColumn("priority", F.lit(9)))
w = Window.partitionBy("match_key").orderBy("priority", F.desc_nulls_last("document_count"), "canonical_token")
dictionary = (from_catalogue.unionByName(from_codes).where(F.length("match_key") > 0)
              .withColumn("_rn", F.row_number().over(w)).where("_rn = 1")
              .select("match_key", "canonical_token", "pattern_id"))
dictionary.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(DICTIONARY)
dictionary = spark.table(DICTIONARY).cache()
logger.info("written to %s: %d match keys", DICTIONARY, dictionary.count())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality checks
# MAGIC One key per row and environment, a known pattern on every row, a canonical token exactly on the `CANONICAL_TOKEN` rows, a REF on every REF row, every token of the codes reachable in the dictionary. A failed check stops the notebook.

# COMMAND ----------

checks = {
    "one row per env and code_key":
        result.groupBy("env", "code_key").count().where("count > 1").count(),
    "pattern_id in the catalogue":
        result.where(~F.col("pattern_id").isin(PATTERN_ORDER) | F.col("treatment").isNull()).count(),
    "canonical_token and match_key set exactly on CANONICAL_TOKEN":
        result.where((F.col("treatment") == "CANONICAL_TOKEN")
                     != (F.col("canonical_token").isNotNull() & F.col("match_key").isNotNull())).count(),
    "catalogue_ref set on REF-* and UNL-01":
        result.where((F.col("pattern_id").startswith("REF-") | (F.col("pattern_id") == "UNL-01"))
                     & F.col("catalogue_ref").isNull()).count(),
    "every match_key of a code in the dictionary":
        coded.select("match_key").distinct().join(dictionary, "match_key", "left_anti").count(),
    "dictionary keys are upper-case letters and digits":
        dictionary.where(~F.col("match_key").rlike("^[A-Z0-9]+$") | ~F.col("canonical_token").rlike("^[A-Z0-9]+$"))
        .count(),
}
for name, bad in checks.items():
    logger.info("%s %s: %d rows", "OK  " if bad == 0 else "FAIL", name, bad)
failed = [name for name, bad in checks.items() if bad]
if failed:
    raise AssertionError(f"quality checks failed: {failed}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes per pattern
# MAGIC One row per environment and pattern, in evaluation order: number of codes, codes cited in at least one document, codes typed in the chat, and the 8 codes cited by the most documents.

# COMMAND ----------

order = F.array_position(F.array(*[F.lit(p) for p in PATTERN_ORDER]), F.col("pattern_id"))
display(result.groupBy("env", "pattern_id", "pattern_name", "treatment")
        .agg(F.count("*").alias("codes"),
             F.sum(F.when(F.col("document_count") > 0, 1).otherwise(0)).alias("codes_in_documents"),
             F.sum(F.when(F.col("question_count") > 0, 1).otherwise(0)).alias("codes_in_questions"),
             F.slice(F.sort_array(F.collect_list(F.struct("document_count", F.col("spellings")[0].alias("spelling"))),
                                  asc=False), 1, 8).alias("examples"))
        .withColumn("_o", order).orderBy("env", "_o").drop("_o"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## REF formats in the catalogue
# MAGIC Number of catalogue REFs per format, with examples. `REF-99` lists the REFs that match none of the twelve formats.

# COMMAND ----------

formats = None
for env in ENVS:
    part = (spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL").distinct()
            .select(F.lit(env).alias("env"), "ref", ref_format("ref").alias("pattern_id")))
    formats = part if formats is None else formats.unionByName(part)
display(formats.groupBy("env", "pattern_id")
        .agg(F.count("*").alias("refs"), F.slice(F.array_sort(F.collect_set("ref")), 1, 12).alias("examples"))
        .orderBy("env", "pattern_id"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Chat codes not given a token
# MAGIC Codes typed in the chat that keep their text as is. Expected here: glued codes (`GLU-01`) and true noise. A wanted code in this list means an exclusion pattern is too wide.

# COMMAND ----------

display(result.where((F.col("question_count") > 0) & (F.col("treatment") == "KEEP_AS_IS"))
        .select("env", "pattern_id", "code_key", "spellings", "question_count", "document_count")
        .orderBy("env", "pattern_id", F.desc("question_count")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Chart 1 · Catalogue REF citations by format
# MAGIC For each REF format, citations of a catalogue REF in the parsed documents: written exactly as the REF, or written another way (spaces, case, dots: `MI - 1365`, `IF2.03.08`). The second part is what keyword search misses today. Source: section `A_reference` of the inventory (occurrences, not documents).

# COMMAND ----------

import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

cited = (spark.table(SOURCE).where((F.col("section") == "A_reference") & F.col("env").isin(ENVS))
         .select("env", ref_format("ref_catalogue").alias("pattern_id"),
                 F.col("occ").alias("exact"), F.col("occ_autre_graphie").alias("other"))
         .groupBy("env", "pattern_id").agg(F.sum("exact").alias("exact"), F.sum("other").alias("other"))
         .orderBy("env", "pattern_id").toPandas())
fig, axes = plt.subplots(1, len(ENVS), figsize=(7 * len(ENVS), 5), squeeze=False)
for ax, env in zip(axes[0], ENVS):
    d = cited[cited["env"] == env].sort_values("pattern_id", ascending=False)
    ax.barh(d["pattern_id"], d["exact"], color="#4C78A8", label="exact spelling")
    ax.barh(d["pattern_id"], d["other"], left=d["exact"], color="#F58518", label="other spelling")
    for y, (e, o) in enumerate(zip(d["exact"], d["other"])):
        if e + o:
            ax.text(e + o, y, f"  {100 * o / (e + o):.0f} %", va="center", fontsize=8)
    ax.set_title(f"{env.upper()}: citations of catalogue REFs (share in another spelling)")
    ax.set_xlabel("citations in parsed documents")
    ax.legend(loc="best")
plt.tight_layout()
display(fig)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Chart 2 · Codes typed in the chat, by pattern
# MAGIC For each pattern, the distinct codes typed in the chat: cited in at least one parsed document (the canonical token can bring that document up), or absent from the corpus (no passage to find, whatever the treatment).

# COMMAND ----------

chat = (result.where(F.col("question_count") > 0)
        .groupBy("env", "pattern_id")
        .agg(F.sum(F.when(F.col("document_count") > 0, 1).otherwise(0)).alias("found"),
             F.sum(F.when(F.col("document_count") == 0, 1).otherwise(0)).alias("absent"))
        .withColumn("_o", order).orderBy("env", "_o").toPandas())
fig, axes = plt.subplots(1, len(ENVS), figsize=(7 * len(ENVS), 6), squeeze=False)
for ax, env in zip(axes[0], ENVS):
    d = chat[chat["env"] == env].iloc[::-1]
    ax.barh(d["pattern_id"], d["found"], color="#54A24B", label="in at least one document")
    ax.barh(d["pattern_id"], d["absent"], left=d["found"], color="#BAB0AC", label="absent from the corpus")
    ax.set_title(f"{env.upper()}: codes typed in the chat")
    ax.set_xlabel("distinct codes")
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="best")
plt.tight_layout()
display(fig)
