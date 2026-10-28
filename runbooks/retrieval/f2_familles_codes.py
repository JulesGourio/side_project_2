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
# MAGIC | `KEEP_AS_IS` | text indexed unchanged: already one token, or not a code | `LEVEL 2`, `IS0024879M` |
# MAGIC | `GLOSSARY_CANDIDATE` | letters-only acronym, unchanged; input for the acronym glossary | `DOA`, `EASA` |
# MAGIC
# MAGIC **Safety rule.** `KEEP_AS_IS` leaves the text exactly as it is indexed today: a code put there by mistake is never found worse than today, only not improved. A token is appended only for a key of the dictionary, so a code pattern must name a code, never a common word followed by a number: the code patterns ask for a spelling in upper case, a glued spelling or a separator, and the exclusions of running text (`EXC-11`, `EXC-08`) come before them.

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
# MAGIC | catalogue key | key of a catalogue REF, with and without its language suffix (`FR` `EN` `GB` `MX` `BG` `CZ` `BR` `ES`), and without its final letters | `MI-1413_FR` → `MI1413FR`, `MI1413`; `QIS-46A-352 A` → `QIS46A352` |
# MAGIC | ambiguous key | catalogue key that leads to several REFs with different tokens: it maps to itself | `Q0087QP` (`Q0087QP-EG`, `Q0087QP-G`), `QIS45D008` (revisions `A` and `B`) |
# MAGIC | canonical token | for a REF, its catalogue key without language suffix; for an ambiguous key, the key; otherwise the key | `Q0697MI_FR` → `Q0697MI` |
# MAGIC | code spelling | a spelling that names a code, not a word of running text: an upper-case word of 2 letters or more that is not a common word, an upper-case letter glued to a digit, or a separator `-` `_` `/` `.` next to a letter | `MEP 10-069`, `B1`, `tool_8660197`; not `de 2`, `Inspect 100`, `O 180`, `OF 9` |
# MAGIC
# MAGIC A code is linked to a catalogue REF when its key is a catalogue key, or when one slice of a spelling is the key of a catalogue REF (`MI-13991` in `MI-13991Table 3`, `QP-2086` in `QP-2086-Software`). The longest such slice wins. That slice is the **match key**: the part of the text the matcher recognises and follows with the token. A REF is never linked through a part of it: `IT-243` alone is not `LdB IT-243`, `P100` is not `LdB P-100`.

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
# MAGIC | `REF-04` | Lettre de branchement | `^LDB [A-Z]{1,2}-[0-9]{3}$` | LdB IT, P, CI, F, NF | `LdB IT-243`, `LdB P-155` | `LDB IT 243`, `LdB-P099`, `LdB IT-243`; never without `LdB` | `LDBIT243` |
# MAGIC | `REF-05` | Segmented procedure | `^[A-Z]+[0-9]?-[0-9]{2}-[0-9]+$` | DLV, QTP, QTR, TDAJ, TDA, UDA, UDB, UDC, UDE, R | `DLV-31-5173`, `QTP-01-245`, `UDE2-00-113` | `QTP 01-174`, `DLV31-5117`, `TDA8 - 00/027` | `DLV315173` |
# MAGIC | `REF-06` | QIS | `^QIS-[0-9]{2}[A-Z]-[0-9]{3}( [A-Z])?$` | QIS | `QIS-46A-352 A`, `QIS-45D-208` | `QIS-46A-352`, `QIS-46B-035-a` | `QIS46A352A` when the catalogue has one revision; the key without revision when it has several |
# MAGIC | `REF-07` | IF instruction | `^IF2[0-9]{4}(_[A-Z]{2})?$` | IF | `IF20322_BR`, `IF20168` | `IF2.03.08_CZ`, `IF 2.03.16`, `IF 20316` | `IF20322` |
# MAGIC | `REF-08` | Glued internal reference | `^[A-Z]{2,5}[0-9]{3,4}(-[0-9])?([_.][A-Z]{2}){0,2}$` | PRLAT, INAQ, INE, FDAQL | `PRLAT566_FR`, `INE028_FR_EN` | `PR/LAT/506`, `IN/AQ/561`, `FD_AQL_2193`, `PRLAT528.FR` | `PRLAT566` |
# MAGIC | `REF-09` | IQ register | `^IQ[-_]?[0-9]{2}[-_][0-9]{3,5}(_[A-Z][0-9]{2})?(_[A-Z]{2})?$` | IQ | `IQ20_384_EN`, `IQ_20_256_EN`, `IQ14_513_R30_FR` | `IQ 26-1450`, `IQ19-807` | `IQ20384` |
# MAGIC | `REF-10` | Programme phase | `^(PRO\|PROLAT\|OBS)[-_][A-Z][0-9][0-9A-Z]?([-_][A-Z]+)?$` | PRO, PROLAT, OBS | `PRO-R80`, `PROLAT_R40_FR`, `OBS_A8P` | `PRO R80` | `PROLATR40` |
# MAGIC | `REF-11` | Spaced segments | `^[A-Z]{3}( [0-9]{2}){2,3}$` | IAQ, RAQ, PAQ | `IAQ 04 04 05`, `PAQ 09 21` | `IAQ04 04 05`, `PAQ 10.03` | `IAQ040405` |
# MAGIC | `REF-12` | Numeric | `^[0-9]{2,3}([- ]+-?[0-9]+)+$` | none | `115-000-00-108`, `19 --058` | `190.62`, `19058` | `11500000108` |
# MAGIC | `REF-99` | Other catalogue REF | none: exact catalogue key | ŘLCZ, 777F, L, A1O, IN, MQP, LMX, DAP, … | `ŘLCZ-07/18`, `777F-MDCD-QP`, `L_1311_CMP` | as for the others | key of the REF |
# MAGIC
# MAGIC A catalogue key without final letters that several REFs share (`777F`, `A8B`, `F0005F`, `FORM607`, `H0029MI`, `Q0087QP`, `QIS45D008`) keeps the format of the first of them, maps to itself, and lists the REFs in `candidate_refs`; `catalogue_ref` and `intraqual_url` stay empty.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Other codes
# MAGIC
# MAGIC Codes that are not linked to a catalogue REF. *Key* regexes apply to the key (upper case, separators removed), *spelling* regexes to every spelling as written.
# MAGIC
# MAGIC | ID | Pattern | Regex or condition | Captures | Examples | Treatment |
# MAGIC | --- | --- | --- | --- | --- | --- |
# MAGIC | `ACR-02` | Separated acronym | spelling, case-sensitive: `^[A-Z]([./][A-Z]){2,}\.?$`; key not a common word (`EXC-13`) | one acronym of 3 letters or more written letter by letter with dots or slashes; two letters (`A/B`, `Y/N`, `I.E`) are mostly pairs and ranges, and two acronyms joined by a slash (`NDT/NDI`) are two words already: both stay `ACR-01` | `E.A.S.A`, `N.D.T`, `M.O.E`, `T.V.A` | `CANONICAL_TOKEN` (`[EASA]`) |
# MAGIC | `ACR-01` | Acronym | key: `^[A-Z]+$`, 6 letters at most, not a common word (`EXC-13`); `glossary_key` gives the singular of a plural (`KPIS` → `KPI`) | letters-only words in upper case or mixed case (`eFNC` → `EFNC`) | `DOA`, `EASA`, `NADCAP`, `PFMEA` | `GLOSSARY_CANDIDATE` |
# MAGIC | `UNL-01` | Unlisted REF | a slice, language suffix removed, has the signature of a catalogue REF (leading letters, then at least one digit), and the rest of the key holds 3 characters at most, or the slice starts the spelling and holds a separator (`MI-13991` in `MI-13991Table 3`); never a slice inside a longer code (`F05703` in `STD-0003415-F05703A`) | REFs cited but absent from the catalogue: obsolete, other site, typo | `SF-1027` (as `SF-1029`), `NS-1717`, `MI-13663`, `PAQ 10 03` | `CANONICAL_TOKEN` |
# MAGIC | `TRM-01` | Method or term | key in `TERMS` | quality methods and drawing types written with a digit | `8D`, `5S`, `3D`, `2D` | `CANONICAL_TOKEN` |
# MAGIC | `MAT-01` | Material or alloy | key: `^([0-9]{4}T[0-9]+$\|[0-9]{3}PH$\|TA[0-9]+V\|TI[0-9]\|[XZ][0-9]+C[RN])` | aluminium tempers, steels, titanium alloys | `2024-T3`, `7175-T7351`, `15-5PH`, `TA6V`, `Ti-6Al-4V`, `Z6CNU17-04` | `CANONICAL_TOKEN` |
# MAGIC | `STD-01` | Standard, specification or ATA chapter | key: `^(EN\|PREN\|ISO\|AS\|NAS\|NSA\|MS\|ABS\|ASNA\|BAC\|BACB\|BACS\|BSS\|BAPS\|BMS\|HJPS\|NE\|NF\|AIPI\|AIPS\|AITM\|IPDA\|DO\|ED\|ARP\|AMS\|ASTM\|MIL\|DIN\|SAE\|IPC\|IEC\|LN\|HST\|HL\|CCR\|ATA)[0-9]`; one spelling with the prefix in upper case: `^(pr)?[A-Z]{2,5}[-_/. ]{0,3}[0-9]` | aerospace, Airbus, Boeing, Dassault standards, part standards, ATA chapters | `EN 9100`, `AS9100`, `BAC 5578`, `NE 40-012`, `prEN 3155`, `ATA52`; not `en 2021`, `do 15` | `CANONICAL_TOKEN` |
# MAGIC | `REG-01` | Regulation | key: `^((PART\|PARTIE)(21\|145\|147\|66\|M\|ML\|CAMO)([A-Z][A-Z0-9]*)?\|CS(2[2-9]\|3[46]\|E\|P\|APU\|ETSO\|AWO\|VLA\|VLR\|LSA\|ACNS\|FCD\|MMEL)[A-Z0-9]*\|(AMC\|GM)[0-9]*(21\|145\|147\|66\|M\|ML\|CS\|20\|25)[A-Z0-9]*\|21[GJ])$` | EASA parts, certification specifications, AMC and GM, Part 21 subparts | `PART 145`, `Part-21`, `CS 25-1309`, `AMC1 21`, `21-J`, `Partie-145.A.90`; not `PART 1`, `PARTIE 3`, `part 1/4` | `CANONICAL_TOKEN` |
# MAGIC | `ACF-01` | Aircraft or programme | key in a closed list: `^(A(220\|300\|310\|318\|319\|320\|321\|330\|340\|350\|380)(NEO\|XLR\|XWB\|[0-9]{3,4})?\|A400M\|(B\|BOEING)7[0-9]7([0-9]{1,3}\|MAX[0-9]{0,2})?\|F(7X\|8X\|6X\|10X\|900\|2000)(EX\|LX\|DX)?\|ATR(42\|72)([0-9]{2,3})?\|ERJ[0-9]{3}\|E(170\|175\|190\|195)(E2)?\|CRJ[0-9]{3,4})$`; one spelling starting in upper case, without a decimal number | Airbus, Boeing, Dassault, ATR, Embraer, Bombardier families | `A320`, `A 350`, `A400M`, `B 787`, `F7X`, `ATR 42/72`; not `a 905`, `A000`, `a 12.5` | `CANONICAL_TOKEN` |
# MAGIC | `SAP-01` | SAP transaction | key: `^(SE\|SM\|SU\|SP\|ST\|SQ\|ME\|MM\|MB\|VA\|VL\|VF\|CO\|QA\|QE\|QM\|QS\|IW\|IE\|IL\|CJ\|CN\|KO\|KS\|MD\|XK\|ZQ\|ZM)[0-9]{2}[A-Z]?$`; one spelling glued in upper case | transaction codes | `SE16N`, `ME21N`, `QA32`, `MM03` | `CANONICAL_TOKEN` |
# MAGIC | `SHC-01` | Short code | key: `^[A-Z]{1,2}[0-9]{1,2}$`; one spelling glued or with a hyphen, in upper case: `^[A-Z]{1,2}-?[0-9]{1,2}$` | licence categories, phases, zones, chapters | `B1`, `R-30`, `D1`, `CH15`, `ZP09`; not `B 2` alone | `CANONICAL_TOKEN` |
# MAGIC | `IDN-01` | Segmented identifier | one spelling: `^[A-Z][A-Za-z0-9]*([-_/. ]{1,3}[A-Za-z0-9]+)+$` with 3 consecutive digits, and two upper-case letters in a row or a separator `-` `_` `/` `.` | internal numbers, tools, lots, drawings | `STM-0002396`, `HAR-998`, `MEP 10-069`, `IN.ME.L269` | `CANONICAL_TOKEN` |
# MAGIC | `PN-01` | Part number | key: `^[A-Z][0-9]{7,}$` | part numbers written as one letter and digits | `L0025939`, `V53123000000` | `CANONICAL_TOKEN` |
# MAGIC | `GLU-01` | Glued code | no spelling contains a separator | part and serial numbers written as one word | `IS0024879M`, `S02P02`, `F04641A` | `KEEP_AS_IS` (already one token) |
# MAGIC | `OTH-01` | Other code | letters and digits matched by no other pattern, 20 characters at most | long tail: labels, tool references, mixed spellings | `C3-Fill`, `tool_8660197`, `BOE_BACC65CR3PN` | `CANONICAL_TOKEN` |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Excluded tokens
# MAGIC
# MAGIC Narrow patterns for tokens that are not codes. They all keep `KEEP_AS_IS`.
# MAGIC
# MAGIC | ID | Pattern | Regex or condition | Source in documents | Examples |
# MAGIC | --- | --- | --- | --- | --- |
# MAGIC | `EXC-13` | Common word in upper case | letters-only key in `COMMON_WORDS` (function words in French, English, Spanish, Portuguese and frequent document words), or of 7 letters or more | titles and headers in upper case | `DE`, `BY`, `DES`, `VALIDATION`, `DOCUMENTS`, `LATECOERE` |
# MAGIC | `NUM-01` | Number only | key: `^[0-9]+$` | section numbers, decimals, dates | `4.2.1`, `6.1`, `24032022` |
# MAGIC | `EXC-11` | Word and number in running text | every spelling: `^[A-Za-z]+ +[0-9]`, and none starts with an upper-case word of 2 letters or more other than a word of `STOPWORDS`; aircraft of `ACF-01` excepted | a word of running text, in any case, followed by a number | `de 2`, `of 9`, `OF 9`, `TO 100`, `Inspect 100`, `O 180`, `en 2021`, `a 905`, `part 1/4`, `Division 3000` |
# MAGIC | `EXC-09` | URL, path or file name | one spelling: `^https?`, `^www\.`, `\.(aspx?\|local\|corp\|com\|net\|org\|html?\|pdf\|docx?\|xlsx?\|pptx?\|png\|jpe?g\|txt\|csv)($\|[/.?#])`, or a lower-case `/path/` | links, file references | `prtouintraqual1/intraqual/identification.aspx`, `20LBG_B.pdf` |
# MAGIC | `EXC-01` | Table column label | key: `^(COL\|OA\|OC)[0-9]+$` | spreadsheet headers | `COL6`, `OA21`, `OC20` |
# MAGIC | `EXC-02` | Heading number glued to a word | key: `^[0-9]+[A-Z]{3,}` | tables of contents | `2SCOPE4`, `1OBJECT4`, `11REFERENCE` |
# MAGIC | `EXC-03` | Measure unit or ordinal | key: `^[0-9]+(MM\|CM\|…\|ST\|ND\|RD\|TH\|ER\|EME\|E)$`, units of `UNITS`; the terms of `TRM-01` excepted | quantities, ordinals | `5MM`, `60KHZ`, `1ST`, `2ND` |
# MAGIC | `EXC-04` | Dimension | key: `^[0-9]+(X[0-9]+)+$` | sizes | `125x80`, `76X254X1` |
# MAGIC | `EXC-10` | Code and a separate number | every spelling: a word with letters and digits, then spaces and a word starting with a digit: `^(?=\S*[0-9])(?=\S*[A-Za-z])\S+ +[0-9]\S*$` | a code followed by a quantity or another code | `AMS5862 15-5PH`, `7475-T7351 2.500`, `6061-O 0.050`, `AMC1 145` |
# MAGIC | `EXC-08` | Word and number, never glued | every spelling: `^[A-Za-z]+ +[0-9][0-9 ./,:-]*$`; one spelling has a lower-case letter in its word, or every word has 5 letters or more; regulations and aircraft (`Part 21`, `Boeing 787`) excepted | running text, dates after a word | `inspect 100`, `Le 12/11/2019`, `PROTECTION 999` |
# MAGIC | `EXC-05` | List item or section | every spelling: `^([0-9]{1,3}[.)-]){0,3}[0-9]{1,3}[-_/. )]?[A-Za-z]{1,2}$` | numbered bullets and sections | `3.P`, `1-b`, `10P`, `5.3.Po`, `6.2.R` |
# MAGIC | `EXC-06` | Date with month name | one spelling: a month name of `MONTHS` (French, English, Spanish, Portuguese, full or abbreviated) not followed by a letter, up to 3 separators, a digit | dates | `July 2024`, `Jan/31/2024`, `JANVIER2018`; not `MARKING8`, `MAINTENANCE50`, `JANTX1N5554` |
# MAGIC | `EXC-07` | Word and small number | key `^[A-Z]{3,}[0-9]{1,2}$`, or every spelling `^[A-Za-z]{3,}[-_/. ]{0,3}[0-9]{1,2}([-_/. ]{1,3}[0-9]{1,2})*$`, the word not the leading letters of a catalogue REF; or a short code of `SHC-01` never written glued or with a hyphen | levels, steps, figures, tables, document parts | `LEVEL 2`, `NIVEAU 3`, `TABLE 1/1`, `Figure 13`, `PART 1`, `PARTIE 3`, `B 2` |
# MAGIC | `EXC-12` | Long concatenation | key longer than 20 characters | table cells run together | `101A002SL001D9251276`, `5000D92515131000AB` |
# MAGIC
# MAGIC **Not excluded on purpose.** A standard glued to a heading number (`4179NAS410`, `9001ISO14001`) stays `EXC-02`: the matcher appends tokens to whole words only, so a token for the standard inside such a word would never be written.
# MAGIC
# MAGIC ## Evaluation order
# MAGIC
# MAGIC First match wins: `REF-01` … `REF-99`, `EXC-13`, `ACR-02`, `ACR-01`, `NUM-01`, the Part 21 subparts `21G` and `21J` (`REG-01`), `EXC-11`, `UNL-01`, `EXC-09`, `EXC-01`, `EXC-02`, `TRM-01`, `EXC-03`, `EXC-04`, `EXC-10`, `EXC-08`, `MAT-01`, `EXC-05`, `EXC-06`, `STD-01`, `REG-01`, `ACF-01`, `SAP-01`, `SHC-01`, `EXC-07`, `EXC-12`, `IDN-01`, `PN-01`, `GLU-01`, `OTH-01`.
# MAGIC
# MAGIC Catalogue REFs come before every exclusion. Running text (`EXC-11`) comes before the unlisted REFs and every code pattern, so that no common word followed by a number becomes a code. The Part 21 subparts come before the units and list items so that `21G` and `21J` stay codes, the terms of `TRM-01` before the units so that `8D` stays one, materials before the list items so that `15-5PH` stays one, `EXC-07` and `EXC-12` before the catch-all patterns `IDN-01` and `OTH-01`.
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
# MAGIC | `canonical_token` | string | token appended to the text, equal to the dictionary token of `match_key`; null unless `CANONICAL_TOKEN` |
# MAGIC | `match_key` | string | key the matcher recognises in the text: the key, or the slice linked to a REF (`MI13991` for `MI-13991Table 3`); null unless `CANONICAL_TOKEN` |
# MAGIC | `catalogue_ref` | string | the catalogue REF of a `REF-*` code; null for an ambiguous key and for every other pattern |
# MAGIC | `candidate_refs` | array&lt;string&gt; | the REFs an ambiguous key leads to; null otherwise |
# MAGIC | `family_ref` | string | `UNL-01` only: one catalogue REF with the same signature, never the document of the code |
# MAGIC | `intraqual_url` | string | Intraqual link of `catalogue_ref` |
# MAGIC | `glossary_key` | string | `ACR-01` and `ACR-02` only: the acronym, singular for a plural (`KPIS` → `KPI`) |
# MAGIC | `document_count` | long | parsed documents citing the code |
# MAGIC | `question_count` | long | chat questions citing the code |
# MAGIC | `top_documents` | array&lt;string&gt; | the 10 document REFs citing it most, `REF (n)` |
# MAGIC
# MAGIC ## Dictionary `dev_proj.qualibot.f3_cles_codes`
# MAGIC
# MAGIC The matcher appends a token only for a key of this dictionary. One row per match key, for DEV and UAT together, since one dictionary serves both:
# MAGIC
# MAGIC | Column | Content |
# MAGIC | --- | --- |
# MAGIC | `match_key` | key recognised in the text: every catalogue key of every environment (cited or not, so that a question finds it); the `match_key` and the `canonical_token` of every `CANONICAL_TOKEN` code |
# MAGIC | `canonical_token` | token appended after it: catalogue REF without language suffix for a REF, `canonical_token` of the code otherwise, the match key itself when the key is ambiguous |
# MAGIC | `pattern_id` | format of the REF, or `pattern_id` of the code |
# MAGIC | `source` | `catalogue` (key of a catalogue REF) or `corpus` (code of the documents or questions) |
# MAGIC | `envs` | environments where the key was found: `["dev", "uat"]` |
# MAGIC | `alternatives` | the tokens an ambiguous key could expand to; null otherwise |
# MAGIC
# MAGIC A key found both ways keeps the catalogue row. A key that leads to two tokens or more, within one environment or across both, maps to itself.

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

# Pattern catalogue: the regexes and word lists of the tables above, used as is by the classification

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
                "DIN", "SAE", "IPC", "IEC", "LN", "HST", "HL", "CCR", "ATA"]
UNITS = ["MM", "CM", "DM", "M", "KM", "UM", "ML", "CL", "L", "MG", "G", "KG", "N", "NM", "DAN", "KN", "BAR", "MBAR",
         "PSI", "PA", "KPA", "MPA", "HZ", "KHZ", "MHZ", "GHZ", "DB", "V", "KV", "MV", "A", "MA", "W", "KW", "H",
         "MIN", "MN", "S", "C", "K", "F", "IN", "FT", "LB", "LBS", "OZ", "PC", "PCS", "PT", "D", "X",
         "ST", "ND", "RD", "TH", "ER", "ERE", "EME", "E", "IEME", "O"]
TERMS = ["2D", "3D", "5S", "8D"]
PART21_SUBPARTS = ["21G", "21J"]
SAP_PREFIXES = ["SE", "SM", "SU", "SP", "ST", "SQ", "ME", "MM", "MB", "VA", "VL", "VF", "CO", "QA", "QE", "QM", "QS",
                "IW", "IE", "IL", "CJ", "CN", "KO", "KS", "MD", "XK", "ZQ", "ZM"]
MONTHS = ["JANUARY", "JANVIER", "JANEIRO", "ENERO", "JANV", "JAN",
          "FEBRUARY", "FEVRIER", "FÉVRIER", "FEVEREIRO", "FEBRERO", "FEVR", "FÉVR", "FEB", "FEV", "FÉV",
          "MARCH", "MARS", "MARZO", "MARÇO", "MARCO", "MAR",
          "APRIL", "AVRIL", "ABRIL", "AVR", "APR", "ABR",
          "MAY", "MAI", "MAYO", "MAIO",
          "JUNE", "JUIN", "JUNIO", "JUNHO", "JUN",
          "JULY", "JUILLET", "JULIO", "JULHO", "JUIL", "JUL",
          "AUGUST", "AOUT", "AOÛT", "AGOSTO", "AUG", "AOU", "AGO",
          "SEPTEMBER", "SEPTEMBRE", "SEPTIEMBRE", "SETEMBRO", "SEPT", "SEP", "SET",
          "OCTOBER", "OCTOBRE", "OCTUBRE", "OUTUBRO", "OCT", "OUT",
          "NOVEMBER", "NOVEMBRE", "NOVIEMBRE", "NOVEMBRO", "NOV",
          "DECEMBER", "DECEMBRE", "DÉCEMBRE", "DICIEMBRE", "DEZEMBRO", "DEC", "DÉC", "DIC", "DEZ"]
# Function words of French, English, Spanish and Portuguese; standard prefixes (EN, AS, DO, NE) are left out.
STOPWORDS = ["A", "AL", "AN", "AND", "AO", "AOS", "ARE", "AT", "AU", "AUX", "AVEC", "BE", "BY", "CA", "CE", "CES",
             "CON", "DA", "DANS", "DAS", "DE", "DEL", "DES", "DOS", "DU", "E", "EL", "EM", "ES", "ET", "FOR", "FROM",
             "IF", "IL", "INTO", "IS", "LA", "LAS", "LE", "LES", "LOS", "NA", "NO", "NOS", "NUM", "O", "OF", "ON",
             "OR", "OS", "OU", "PAR", "PARA", "PELA", "PELO", "PER", "POR", "POUR", "QUE", "QUI", "SA", "SE", "SEE",
             "SES", "SI", "SIN", "SOIT", "SON", "SU", "SUR", "SUS", "THE", "TO", "UM", "UMA", "UN", "UNA", "UNE",
             "UNO", "UP", "VOIR", "WITH", "Y"]
# Upper-case words of titles and headers that are not acronyms.
COMMON_WORDS = sorted(set(STOPWORDS) | {
    "EN", "AS", "DO", "NE", "IN", "IT", "NON", "OUI", "YES", "OK", "ALL", "NOT", "BUT", "PAS", "MAIS", "TOUT",
    "TOUS", "DATE", "PAGE", "NOTE", "NOTES", "TITLE", "TITRE", "TABLE", "NOM", "NAME", "VISA", "TYPE", "CODE",
    "REF", "REV", "REVUE", "SITE", "LIEU", "PIECE", "PART", "PARTS", "LOT", "LOTS", "FIN", "END", "VIA", "PLAN",
    "PLANS", "ZONE", "ZONES", "AIR", "SANS", "SOUS", "LORS", "AFIN", "ETC", "CF", "VS", "PRO", "RAS", "NB", "OBS"})
ACR_MAX_LETTERS = 6
UNL_MAX_EXTRA = 3
MAX_CODE_LENGTH = 20

RX = {
    "ACR-02": r"^[A-Z]([./][A-Z]){2,}\.?$",
    "ACR-01": r"^[A-Z]+$",
    "NUM-01": r"^[0-9]+$",
    "EXC-11": r"^[A-Za-z]+ +[0-9]",
    "EXC-11-code": r"^[A-Z]{2,} +[0-9]",
    "EXC-09": (r"(?i)(^https?|^www\.|\.(aspx?|local|corp|com|net|org|html?|pdf|docx?|xlsx?|pptx?|png|jpe?g|txt|csv)"
               r"($|[/.?#])|(?-i:/[a-z]{2,}/))"),
    "EXC-01": r"^(COL|OA|OC)[0-9]+$",
    "EXC-02": r"^[0-9]+[A-Z]{3,}",
    "EXC-03": f"^[0-9]+({'|'.join(sorted(set(UNITS), key=len, reverse=True))})$",
    "EXC-04": r"^[0-9]+(X[0-9]+)+$",
    "EXC-10": r"^(?=\S*[0-9])(?=\S*[A-Za-z])\S+ +[0-9]\S*$",
    "EXC-08": r"^[A-Za-z]+ +[0-9][0-9 ./,:-]*$",
    "MAT-01": r"^([0-9]{4}T[0-9]+$|[0-9]{3}PH$|TA[0-9]+V|TI[0-9]|[XZ][0-9]+C[RN])",
    "EXC-05": r"^([0-9]{1,3}[.)-]){0,3}[0-9]{1,3}[-_/. )]?[A-Za-z]{1,2}$",
    "EXC-06": f"(?iu)^({'|'.join(sorted(set(MONTHS), key=len, reverse=True))})(?![A-Za-zÀ-ÿ])[ ./-]{{0,3}}[0-9]",
    "STD-01": f"^({'|'.join(STD_PREFIXES)})[0-9]",
    "STD-01-spelling": r"^(pr)?[A-Z]{2,5}[-_/. ]{0,3}[0-9]",
    "REG-01": (r"^((PART|PARTIE)(21|145|147|66|M|ML|CAMO)([A-Z][A-Z0-9]*)?"
               r"|CS(2[2-9]|3[46]|E|P|APU|ETSO|AWO|VLA|VLR|LSA|ACNS|FCD|MMEL)[A-Z0-9]*"
               r"|(AMC|GM)[0-9]*(21|145|147|66|M|ML|CS|20|25)[A-Z0-9]*|21[GJ])$"),
    "ACF-01": (r"^(A(220|300|310|318|319|320|321|330|340|350|380)(NEO|XLR|XWB|[0-9]{3,4})?|A400M"
               r"|(B|BOEING)7[0-9]7([0-9]{1,3}|MAX[0-9]{0,2})?|F(7X|8X|6X|10X|900|2000)(EX|LX|DX)?"
               r"|ATR(42|72)([0-9]{2,3})?|ERJ[0-9]{3}|E(170|175|190|195)(E2)?|CRJ[0-9]{3,4})$"),
    "SAP-01": f"^({'|'.join(SAP_PREFIXES)})[0-9]{{2}}[A-Z]?$",
    "SAP-01-spelling": r"^[A-Z]{2}[0-9]{2}[A-Z]?$",
    "SHC-01": r"^[A-Z]{1,2}[0-9]{1,2}$",
    "SHC-01-spelling": r"^[A-Z]{1,2}-?[0-9]{1,2}$",
    "EXC-07": r"^[A-Z]{3,}[0-9]{1,2}$",
    "EXC-07-spelling": r"^[A-Za-z]{3,}[-_/. ]{0,3}[0-9]{1,2}([-_/. ]{1,3}[0-9]{1,2})*$",
    "IDN-01": r"^[A-Z][A-Za-z0-9]*([-_/. ]{1,3}[A-Za-z0-9]+)+$",
    "IDN-01-code": r"[A-Z]{2}|[-_/.]",
    "PN-01": r"^[A-Z][0-9]{7,}$",
    "GLU-01": r"[-_/. ]",
}

PATTERNS = [
    *[(i, name, rx, "CANONICAL_TOKEN") for i, name, rx in REF_FORMATS],
    (*REF_OTHER, "CANONICAL_TOKEN"),
    ("EXC-13", "Common word in upper case",
     f"letters-only key in COMMON_WORDS or of {ACR_MAX_LETTERS + 1} letters or more", "KEEP_AS_IS"),
    ("ACR-02", "Separated acronym", f"spelling, case-sensitive: {RX['ACR-02']}", "CANONICAL_TOKEN"),
    ("ACR-01", "Acronym", f"{RX['ACR-01']}, {ACR_MAX_LETTERS} letters at most", "GLOSSARY_CANDIDATE"),
    ("NUM-01", "Number only", RX["NUM-01"], "KEEP_AS_IS"),
    ("EXC-11", "Word and number in running text",
     f"every spelling: {RX['EXC-11']}, none {RX['EXC-11-code']} other than a stop word; ACF-01 excepted",
     "KEEP_AS_IS"),
    ("UNL-01", "Unlisted REF",
     f"signature of a catalogue REF: leading letters, 9 per digit, A per letter; at most {UNL_MAX_EXTRA} other "
     "characters in the key, unless the slice starts the spelling and holds a separator", "CANONICAL_TOKEN"),
    ("EXC-09", "URL, path or file name", f"one spelling: {RX['EXC-09']}", "KEEP_AS_IS"),
    ("EXC-01", "Table column label", RX["EXC-01"], "KEEP_AS_IS"),
    ("EXC-02", "Heading number glued to a word", RX["EXC-02"], "KEEP_AS_IS"),
    ("TRM-01", "Method or term", f"key in {TERMS}", "CANONICAL_TOKEN"),
    ("EXC-03", "Measure unit or ordinal", RX["EXC-03"], "KEEP_AS_IS"),
    ("EXC-04", "Dimension", RX["EXC-04"], "KEEP_AS_IS"),
    ("EXC-10", "Code and a separate number", f"every spelling: {RX['EXC-10']}", "KEEP_AS_IS"),
    ("EXC-08", "Word and number, never glued",
     f"every spelling: {RX['EXC-08']}, with a lower-case letter in a word or every word of 5 letters or more, "
     "not REG-01 or ACF-01", "KEEP_AS_IS"),
    ("MAT-01", "Material or alloy", RX["MAT-01"], "CANONICAL_TOKEN"),
    ("EXC-05", "List item", f"every spelling: {RX['EXC-05']}", "KEEP_AS_IS"),
    ("EXC-06", "Date with month name", f"one spelling: {RX['EXC-06']}", "KEEP_AS_IS"),
    ("STD-01", "Standard, specification or ATA chapter",
     f"{RX['STD-01']}, one spelling {RX['STD-01-spelling']}", "CANONICAL_TOKEN"),
    ("REG-01", "Regulation", RX["REG-01"], "CANONICAL_TOKEN"),
    ("ACF-01", "Aircraft or programme",
     f"{RX['ACF-01']}, one spelling starting in upper case without a decimal number", "CANONICAL_TOKEN"),
    ("SAP-01", "SAP transaction", f"{RX['SAP-01']}, one spelling {RX['SAP-01-spelling']}", "CANONICAL_TOKEN"),
    ("SHC-01", "Short code", f"{RX['SHC-01']}, one spelling {RX['SHC-01-spelling']}", "CANONICAL_TOKEN"),
    ("EXC-07", "Word and small number",
     f"{RX['EXC-07']} or every spelling {RX['EXC-07-spelling']}, word not a REF prefix; or SHC-01 never glued",
     "KEEP_AS_IS"),
    ("EXC-12", "Long concatenation", f"key longer than {MAX_CODE_LENGTH} characters", "KEEP_AS_IS"),
    ("IDN-01", "Segmented identifier",
     f"one spelling: {RX['IDN-01']}, with 3 consecutive digits and {RX['IDN-01-code']}", "CANONICAL_TOKEN"),
    ("PN-01", "Part number", RX["PN-01"], "CANONICAL_TOKEN"),
    ("GLU-01", "Glued code", "no spelling contains a separator", "KEEP_AS_IS"),
    ("OTH-01", "Other code", f"letters and digits, no other pattern, {MAX_CODE_LENGTH} characters at most",
     "CANONICAL_TOKEN"),
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
    """Every run of consecutive pieces of a spelling that the matcher can recognise, the whole spelling included.

    A run that stops between a digit and a glued letter is kept only when it holds a separator, as in the matcher
    (``MI-13991`` in ``MI-13991Table``, never ``F04641`` in ``F04641A``).

    :param col: column of the spelling
    :return: array column of the slices (``PRO_Q0014NF_EN`` gives ``PRO``, ``PRO_Q0014``, ``Q0014NF``, …)
    """
    parts = F.split(col, PIECE_CUT)
    n = F.size(parts)

    def run(i: Column, j: Column) -> Column:
        """Slice of the parts i to j, or null when it stops inside a glued word without a separator.

        :param i: first part, from 1
        :param j: last part
        :return: column of the slice or null
        """
        joined = F.array_join(F.slice(parts, i, j - i + 1), "")
        following = F.when(j < n, F.element_at(parts, j + 1)).otherwise(F.lit(""))
        inside = following.rlike("^[A-Za-z]") & ~joined.rlike(f"{SEPARATOR}")
        return F.when(~inside, joined)

    every_run = F.flatten(F.transform(F.sequence(F.lit(1), n), lambda i: F.transform(F.sequence(i, n),
                                                                                     lambda j: run(i, j))))
    return F.filter(every_run, lambda x: x.isNotNull())


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
    """Keys linked to the catalogue REFs of an environment, one row per key.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns key, rank (1: key of the REF, with or without language suffix; 2: key of the
        REF without its final letters; the lowest rank of a key is kept), refs (the REFs of that rank, sorted), ref
        (the REF, null when the key is ambiguous), canonical (token of the key: the REF without language suffix, or
        the key itself when the REFs give several tokens), tokens (every token the REFs give), fmt (format of the
        first REF)
    """
    cat = spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL").distinct()
    is_code = F.col("key").rlike("[A-Z]") & F.col("key").rlike("[0-9]")
    whole = cat.select(to_key("ref").alias("key"), "ref")
    no_lang = cat.select(without_language("ref").alias("key"), "ref")
    no_final_letters = (cat.select("ref", F.explode(leading_cuts(F.trim("ref"))).alias("start"))
                        .where(to_key(F.expr("substring(trim(ref), length(start) + 1)")).rlike("^[A-Z]+$"))
                        .select(to_key("start").alias("key"), "ref").where(is_code))
    rows = (whole.unionByName(no_lang).withColumn("rank", F.lit(1))
            .unionByName(no_final_letters.withColumn("rank", F.lit(2)))
            .where(F.length("key") > 0))
    best = Window.partitionBy("key")
    tokens = F.array_distinct(F.transform("refs", lambda r: without_language(r)))
    return (rows.withColumn("_best", F.min("rank").over(best)).where(F.col("rank") == F.col("_best"))
            .groupBy("key", "rank").agg(F.array_sort(F.collect_set("ref")).alias("refs"))
            .withColumn("_tokens", tokens)
            .select("key", "rank", "refs",
                    F.when(F.size("_tokens") == 1, F.col("refs")[0]).alias("ref"),
                    F.when(F.size("_tokens") == 1, F.col("_tokens")[0]).otherwise(F.col("key")).alias("canonical"),
                    F.array_sort(F.col("_tokens")).alias("tokens"),
                    ref_format(F.col("refs")[0]).alias("fmt")))


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
    """Pattern, treatment and canonical token of every key of an environment, before the dictionary is applied.

    :param env: environment (dev or uat)
    :return: DataFrame with the columns of the output table
    """
    k = inventory(env)
    cat = catalogue_keys(env).cache()
    own_keys = cat.where("rank = 1 AND ref IS NOT NULL").select(F.col("key").alias("piece_key"),
                                                                F.col("ref").alias("slice_ref"),
                                                                F.col("fmt").alias("slice_fmt"))
    signatures = (cat.where("rank = 1 AND ref IS NOT NULL").select(signature("key").alias("sig"), "ref")
                  .where(F.col("sig").rlike("^[A-Z]+.*9"))
                  .groupBy("sig").agg(F.min("ref").alias("unl_ref")))
    # A slice that starts the spelling and holds a separator is the code itself (MI-13991 in MI-13991Table 3).
    pieces = (k.select("code_key", F.explode("spellings").alias("sp"))
              .select("code_key", "sp", F.explode(slices("sp")).alias("s"))
              .where(F.col("s").rlike("[A-Za-z]") & F.col("s").rlike("[0-9]"))
              .select("code_key", to_key("s").alias("piece_key"), without_language("s").alias("piece_nolang"),
                      (F.col("sp").startswith(F.col("s")) & F.col("s").rlike(SEPARATOR)).alias("anchored"))
              .distinct().cache())
    in_ref = (pieces.join(F.broadcast(own_keys), "piece_key")
              .groupBy("code_key").agg(F.max_by(F.struct("slice_ref", "slice_fmt", "piece_key"),
                                                F.length("piece_key")).alias("v"))
              .select("code_key", "v.slice_ref", "v.slice_fmt", F.col("v.piece_key").alias("slice_key")))
    unlisted = (pieces.where(F.col("anchored") | (F.length("code_key") - F.length("piece_nolang") <= UNL_MAX_EXTRA))
                .select("code_key", "piece_nolang", signature("piece_nolang").alias("sig"))
                .join(F.broadcast(signatures), "sig")
                .groupBy("code_key").agg(F.max_by(F.struct("unl_ref", "piece_nolang"),
                                                  F.length("piece_nolang")).alias("u"))
                .select("code_key", "u.unl_ref", F.col("u.piece_nolang").alias("unl_key")))
    # Leading letters of the catalogue REFs: a word that is one of them is never a plain word (DLV-31).
    ref_prefixes = [r["p"] for r in cat.where("rank = 1")
                    .select(F.regexp_extract("key", "^([A-Z]+)", 1).alias("p")).where("p <> ''").distinct().collect()]
    acronyms = k.where(F.col("code_key").rlike(RX["ACR-01"])).select(F.col("code_key").alias("singular"),
                                                                     F.lit(True).alias("singular_known"))

    key = F.col("code_key")
    prefix = F.regexp_extract(key, "^([A-Z]*)", 1)
    every = lambda rx: F.forall("spellings", lambda s: s.rlike(rx))   # noqa: E731
    some = lambda rx: F.exists("spellings", lambda s: s.rlike(rx))    # noqa: E731
    code_spaced = F.exists("spellings", lambda s: s.rlike(RX["EXC-11-code"])
                           & ~F.upper(F.regexp_extract(s, "^([A-Za-z]+)", 1)).isin(STOPWORDS))
    is_aircraft = key.rlike(RX["ACF-01"]) & F.exists(
        "spellings", lambda s: s.rlike("^[A-Z]") & ~s.rlike("[0-9][.,][0-9]"))
    df = (k.join(cat.select(F.col("key").alias("code_key"), F.col("ref").alias("cat_ref"),
                            F.col("refs").alias("cat_refs"), F.col("canonical").alias("cat_canonical"), "fmt"),
                 "code_key", "left")
          .join(in_ref, "code_key", "left")
          .join(unlisted, "code_key", "left")
          .withColumn("singular", F.expr("substring(code_key, 1, length(code_key) - 1)"))
          .join(acronyms, "singular", "left"))

    pattern = (F.when(F.col("fmt").isNotNull(), F.col("fmt"))
               .when(F.col("slice_fmt").isNotNull(), F.col("slice_fmt"))
               .when(key.rlike(RX["ACR-01"]) & (key.isin(COMMON_WORDS) | (F.length(key) > ACR_MAX_LETTERS)),
                     "EXC-13")
               .when(some(RX["ACR-02"]) & key.rlike(RX["ACR-01"]), "ACR-02")
               .when(key.rlike(RX["ACR-01"]), "ACR-01")
               .when(key.rlike(RX["NUM-01"]), "NUM-01")
               .when(key.isin(PART21_SUBPARTS), "REG-01")
               .when(every(RX["EXC-11"]) & ~code_spaced & ~is_aircraft, "EXC-11")
               .when(F.col("unl_ref").isNotNull(), "UNL-01")
               .when(some(RX["EXC-09"]), "EXC-09")
               .when(key.rlike(RX["EXC-01"]), "EXC-01")
               .when(key.rlike(RX["EXC-02"]), "EXC-02")
               .when(key.isin(TERMS), "TRM-01")
               .when(key.rlike(RX["EXC-03"]), "EXC-03")
               .when(key.rlike(RX["EXC-04"]), "EXC-04")
               .when(every(RX["EXC-10"]), "EXC-10")
               .when(every(RX["EXC-08"]) & (some(r"^[A-Za-z]*[a-z][A-Za-z]* ") | every(r"^[A-Za-z]{5,} "))
                     & ~key.rlike(RX["REG-01"]) & ~is_aircraft, "EXC-08")
               .when(key.rlike(RX["MAT-01"]), "MAT-01")
               .when(every(RX["EXC-05"]), "EXC-05")
               .when(some(RX["EXC-06"]), "EXC-06")
               .when(key.rlike(RX["STD-01"]) & some(RX["STD-01-spelling"]), "STD-01")
               .when(key.rlike(RX["REG-01"]), "REG-01")
               .when(is_aircraft, "ACF-01")
               .when(key.rlike(RX["SAP-01"]) & some(RX["SAP-01-spelling"]), "SAP-01")
               .when(key.rlike(RX["SHC-01"]) & some(RX["SHC-01-spelling"]), "SHC-01")
               .when(key.rlike(RX["SHC-01"])
                     | (~prefix.isin(ref_prefixes) & (key.rlike(RX["EXC-07"]) | every(RX["EXC-07-spelling"]))),
                     "EXC-07")
               .when(F.length(key) > MAX_CODE_LENGTH, "EXC-12")
               .when(F.exists("spellings", lambda s: s.rlike(RX["IDN-01"]) & s.rlike("[0-9]{3}")
                              & s.rlike(RX["IDN-01-code"])), "IDN-01")
               .when(key.rlike(RX["PN-01"]), "PN-01")
               .when(~some(RX["GLU-01"]), "GLU-01")
               .otherwise("OTH-01"))
    pid = F.col("pattern_id")
    is_ref = pid.startswith("REF-")
    from_catalogue = is_ref & F.col("fmt").isNotNull()
    catalogue_ref = F.when(from_catalogue, F.col("cat_ref")).when(is_ref, F.col("slice_ref"))
    candidate_refs = F.when(from_catalogue & F.col("cat_ref").isNull(), F.col("cat_refs"))
    plural = (key.endswith("S") & (F.length(key) >= 4)
              & (F.col("singular_known").isNotNull() | some("[A-Z]s$")))
    glossary_key = F.when(pid.startswith("ACR-"), F.when(plural, F.col("singular")).otherwise(key))
    canonical = (F.when(F.col("treatment") != "CANONICAL_TOKEN", F.lit(None))
                 .when(from_catalogue, F.col("cat_canonical"))
                 .when(is_ref, without_language("slice_ref"))
                 .when(pid == "UNL-01", F.col("unl_key"))
                 .otherwise(key))
    match_key = (F.when(F.col("treatment") != "CANONICAL_TOKEN", F.lit(None))
                 .when(is_ref & ~from_catalogue, F.col("slice_key"))
                 .when(pid == "UNL-01", F.col("unl_key"))
                 .otherwise(key))
    patterns = spark.createDataFrame(PATTERNS, "pattern_id string, pattern_name string, pattern_regex string, "
                                               "treatment string")
    return (df.withColumn("pattern_id", pattern)
            .join(F.broadcast(patterns), "pattern_id", "left")
            .withColumn("catalogue_ref", catalogue_ref)
            .select(F.lit(env).alias("env"), "code_key", "spellings", "pattern_id", "pattern_name", "pattern_regex",
                    "treatment", canonical.alias("canonical_token"), match_key.alias("match_key"), "catalogue_ref",
                    candidate_refs.alias("candidate_refs"),
                    F.when(pid == "UNL-01", F.col("unl_ref")).alias("family_ref"),
                    F.when(F.col("catalogue_ref").isNotNull(),
                           F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace("catalogue_ref", " ", "%20")))
                    .alias("intraqual_url"),
                    glossary_key.alias("glossary_key"),
                    "document_count", "question_count", "top_documents"))


def dictionary_rows(codes: DataFrame) -> DataFrame:
    """Dictionary of match keys built from the catalogues of ``ENVS`` and from the coded rows of the patterns.

    :param codes: classified rows of every environment (columns of the output table)
    :return: DataFrame with the columns match_key, canonical_token, pattern_id, source, envs and alternatives, one
        row per match key
    """
    rows = None
    for env in ENVS:
        part = catalogue_keys(env).select(F.col("key").alias("match_key"), F.col("canonical").alias("canonical_token"),
                                          F.col("tokens").alias("_alts"), F.col("fmt").alias("pattern_id"),
                                          F.col("rank").alias("priority"),
                                          F.lit(None).cast("long").alias("document_count"), F.lit(env).alias("env"),
                                          F.lit("catalogue").alias("source"))
        rows = part if rows is None else rows.unionByName(part)
    coded = codes.where(F.col("treatment") == "CANONICAL_TOKEN")
    cols = ["canonical_token", "pattern_id", "document_count", "env"]
    from_codes = (coded.select("match_key", *cols)
                  .unionByName(coded.select(F.col("canonical_token").alias("match_key"), *cols))
                  .withColumn("_alts", F.array("canonical_token"))
                  .withColumn("priority", F.lit(9)).withColumn("source", F.lit("corpus")))
    rows = rows.unionByName(from_codes).where(F.length("match_key") > 0)
    by_key = Window.partitionBy("match_key")
    best = (rows.withColumn("envs", F.array_sort(F.collect_set("env").over(by_key)))
            .withColumn("_best", F.min("priority").over(by_key)).where(F.col("priority") == F.col("_best")))
    return (best.groupBy("match_key")
            .agg(F.array_sort(F.array_distinct(F.flatten(F.collect_list("_alts")))).alias("_tokens"),
                 F.max_by("pattern_id", F.coalesce("document_count", F.lit(0))).alias("pattern_id"),
                 F.first("source").alias("source"), F.first("envs").alias("envs"))
            .select("match_key",
                    F.when(F.size("_tokens") == 1, F.col("_tokens")[0]).otherwise(F.col("match_key"))
                    .alias("canonical_token"),
                    "pattern_id", "source", "envs",
                    F.when(F.size("_tokens") > 1, F.col("_tokens")).alias("alternatives")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build
# MAGIC Classifies the environments of `ENVS`, builds the dictionary, then writes both tables: the token of each code is the dictionary token of its match key, so the two tables always agree.

# COMMAND ----------

codes = None
for env in ENVS:
    part = classify(env)
    codes = part if codes is None else codes.unionByName(part)
codes = codes.cache()
logger.info("classified: %d codes for %s", codes.count(), ENVS)

dictionary_rows(codes).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(DICTIONARY)
dictionary = spark.table(DICTIONARY).cache()
logger.info("written to %s: %d match keys, %d ambiguous", DICTIONARY, dictionary.count(),
            dictionary.where(F.col("alternatives").isNotNull()).count())

tokens = dictionary.select("match_key", F.col("canonical_token").alias("_token"))
(codes.join(tokens, "match_key", "left")
 .withColumn("canonical_token", F.when(F.col("treatment") == "CANONICAL_TOKEN", F.col("_token")))
 .select(*codes.columns)
 .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET))
result = spark.table(TARGET).cache()
logger.info("written to %s: %d codes for %s", TARGET, result.count(), ENVS)
coded = result.where(F.col("treatment") == "CANONICAL_TOKEN")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality checks
# MAGIC One key per row and environment, a known pattern on every row, a token exactly on the `CANONICAL_TOKEN` rows and equal to the dictionary, a REF on every REF row and never on the unlisted REFs, no lettre de branchement without its prefix, no token for a common word followed by a number, a dictionary of upper-case keys. A failed check stops the notebook.

# COMMAND ----------

dict_tokens = dictionary.select("match_key", F.col("canonical_token").alias("dict_token"))
checks = {
    "one row per env and code_key":
        result.groupBy("env", "code_key").count().where("count > 1").count(),
    "pattern_id in the catalogue":
        result.where(~F.col("pattern_id").isin(PATTERN_ORDER) | F.col("treatment").isNull()).count(),
    "canonical_token and match_key set exactly on CANONICAL_TOKEN":
        result.where((F.col("treatment") == "CANONICAL_TOKEN")
                     != (F.col("canonical_token").isNotNull() & F.col("match_key").isNotNull())).count(),
    "every match_key of a code in the dictionary, with the same token":
        coded.join(dict_tokens, "match_key", "left")
        .where(F.col("dict_token").isNull() | (F.col("dict_token") != F.col("canonical_token"))).count(),
    "REF-* rows: one catalogue_ref, or two candidate_refs or more":
        result.where(F.col("pattern_id").startswith("REF-")
                     & (F.col("catalogue_ref").isNull() == (F.coalesce(F.size("candidate_refs"), F.lit(0)) < 2)))
        .count(),
    "UNL-01 rows: family_ref set, catalogue_ref and intraqual_url empty":
        result.where((F.col("pattern_id") == "UNL-01")
                     & (F.col("family_ref").isNull() | F.col("catalogue_ref").isNotNull()
                        | F.col("intraqual_url").isNotNull())).count(),
    "REF-04 match keys start with LDB":
        dictionary.where((F.col("pattern_id") == "REF-04") & ~F.col("match_key").startswith("LDB")).count(),
    "no token for a spelling made only of a stop word and a number":
        coded.where(~F.col("pattern_id").startswith("REF-") & (F.col("pattern_id") != "ACF-01")
                    & F.forall("spellings", lambda s: F.upper(F.regexp_extract(s, "^([A-Za-z]+) +[0-9]", 1))
                               .isin(STOPWORDS))).count(),
    "ambiguous dictionary keys map to themselves":
        dictionary.where(F.col("alternatives").isNotNull() & (F.col("canonical_token") != F.col("match_key")))
        .count(),
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
# MAGIC ## Dictionary per pattern and source
# MAGIC Match keys of the dictionary per pattern, source and environments, with the keys that map to themselves because they lead to several tokens.

# COMMAND ----------

display(dictionary.groupBy("pattern_id", "source", F.array_join("envs", ",").alias("envs"))
        .agg(F.count("*").alias("match_keys"),
             F.sum(F.when(F.col("alternatives").isNotNull(), 1).otherwise(0)).alias("ambiguous"))
        .orderBy("pattern_id", "source", "envs"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ambiguous keys
# MAGIC Keys that lead to several tokens, so they map to themselves: a citation without its suffix is not tied to one REF.

# COMMAND ----------

display(dictionary.where(F.col("alternatives").isNotNull())
        .select("pattern_id", "match_key", "alternatives", "source", "envs").orderBy("pattern_id", "match_key"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Acronyms
# MAGIC Glossary candidates by number of documents, with their singular and their spellings (mixed case included: `eFNC` gives `EFNC`), then the upper-case words left out of the glossary.

# COMMAND ----------

display(result.where(F.col("pattern_id").isin("ACR-01", "ACR-02"))
        .select("env", "pattern_id", "code_key", "glossary_key", F.slice("spellings", 1, 6).alias("spellings"),
                "document_count", "question_count")
        .orderBy("env", F.desc("document_count")).limit(500))
display(result.where(F.col("pattern_id") == "EXC-13")
        .select("env", "code_key", "document_count").orderBy("env", F.desc("document_count")).limit(200))

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
