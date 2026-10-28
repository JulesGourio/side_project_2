# Databricks notebook source
# MAGIC %md
# MAGIC # Dictionnaire des codes et annotation
# MAGIC
# MAGIC **Deux étages, jamais une grosse regex de codes.**
# MAGIC
# MAGIC 1. **Hors ligne, des règles ciblées** (notebook `f2_familles_codes`) : 23 petites règles, une par famille, rangent chaque clé de l'inventaire en *code à annoter* ou *bruit*. Elles ne servent qu'à décider **quelles clés entrent dans le dictionnaire**.
# MAGIC 2. **À la volée, un seul mécanisme générique** (`code_keys.py`, copié en cellule 2) : le texte est coupé en morceaux (lettres seules ou chiffres seuls), les morceaux voisins sont recollés, et une suite n'est gardée que si sa clé est **dans le dictionnaire**. La plus longue gagne. La clé est ajoutée après le mot (`SF-1027 [SF1027]`), le texte n'est jamais modifié. Même traitement pour les passages et pour la question.
# MAGIC
# MAGIC | Traitement | Où | Regex | Rôle |
# MAGIC |---|---|---|---|
# MAGIC | Découpe en morceaux | `code_keys.py` | `[A-Za-z]+\|[0-9]+` | `Q0375MI` donne `Q`, `0375`, `MI` |
# MAGIC | Écart permis entre morceaux | `code_keys.py` | rien, ou 0 à 3 espaces autour de 1 ou 2 séparateurs (`-` tirets unicode `_` `/` `.`), ou 1 à 3 espaces | `MI - 1365`, `IAQ 04 04 05`, `19 --058`, `TDA8 - 00/027` |
# MAGIC | Début et fin d'un code | `code_keys.py` | début de mot ; fin de mot, ou entre un chiffre et une lettre collés | `MI-13991` dans `MI-13991Table` ; rien dans `XSF-1027` |
# MAGIC | Choix | `code_keys.py` | aucune : recherche dans le dictionnaire, la plus longue gagne | `QIS-46A-352 A` plutôt que `QIS-46A-352` |
# MAGIC | Contenu du dictionnaire | ce notebook, cellule 3 | familles `annoter` de `f2_familles_codes` + REF du catalogue (DEV et UAT) | REF, normes, pièces, codes courts, résidu |
# MAGIC
# MAGIC Écritures : `dev_proj.qualibot.f3_cles_codes` (le dictionnaire) seulement. Lectures : `f2_familles_codes`, `doc_catalog` DEV et UAT, `dev_proj.qualibot.chunks`, `uat_proj.qualibot.chat_turns`. Tes droits suffisent.

# COMMAND ----------

# Cellule 1 · paramètres

import pandas as pd
from pyspark.sql import Window
from pyspark.sql import functions as F

FAMILLES = "dev_proj.qualibot.f2_familles_codes"
CATALOGUES = ["dev_proj.qualibot.doc_catalog", "uat_proj.qualibot.doc_catalog"]
CHUNKS = "dev_proj.qualibot.chunks"
QUESTIONS = "uat_proj.qualibot.chat_turns"
TARGET = "dev_proj.qualibot.f3_cles_codes"
EXEMPLES = 5   # exemples avant / après par famille
LANGUES = ["FR", "EN", "GB", "MX", "BG", "CZ", "BR", "ES"]   # suffixes retirés des REF, comme doc_catalog.canon_ref

# COMMAND ----------

# MAGIC %md
# MAGIC ### Le mécanisme
# MAGIC Copie exacte de `server/services/chat/engine/code_keys.py` (un test du dépôt vérifie que les deux sont identiques).

# COMMAND ----------

# Cellule 2 · mécanisme d'annotation (copie de code_keys.py)

import re
from typing import Iterable, Mapping, NamedTuple

# --- code_keys: start (copied as is into runbooks/retrieval/f3_dictionnaire_codes.py, a test checks it) ---
# Bump when the matching or the key changes: stored annotations are recomputed for the new version.
PP_VERSION = 1

# A piece is letters only or digits only (Q0375MI gives Q, 0375, MI).
_PIECE = re.compile(r'[A-Za-z]+|[0-9]+')
# Gap allowed between two pieces of one code: nothing, up to 3 spaces around one or two separators (hyphens,
# unicode dashes, _ / .), or 1 to 3 spaces alone (IAQ 04 04 05, MI - 1365, 19 --058).
_GAP = re.compile(r'(?:[  ]{0,3}[-‐‑−–—_/.]{1,2}[  ]{0,3}|[  ]{1,3})?')
# Longest code, in pieces (IF20037_DLV-31-5207 has 6).
_MAX_PIECES = 12


class CodeMatch(NamedTuple):
    """One known code found in a text."""

    start: int
    end: int
    key: str


def _word_end(text: str, pos: int) -> int:
    """End of the word that contains ``pos``; ``_`` joins a word, as in the keyword tokenizer (``Q0697MI_FR``).

    :param text: text
    :param pos: position in the text
    :return: index of the first character after the word
    """
    while pos < len(text) and (text[pos].isalnum() or text[pos] == '_'):
        pos += 1
    return pos


class CodeKeys:
    """Finds the known codes of a text and their glued key."""

    def __init__(self, keys: Mapping[str, str] | Iterable[str]) -> None:
        """Builds the dictionary of keys.

        :param keys: glued upper-case keys (``SF1027``), or a mapping from the glued spelling found in a text to the
            key written for it, so that every spelling of one document gives one key (``LDBIT243`` and ``IT243``
            both give ``LDBIT243``, ``Q0697MIFR`` gives ``Q0697MI``)
        """
        pairs = keys.items() if isinstance(keys, Mapping) else ((k, k) for k in keys)
        self.keys = {k.strip().upper(): v.strip().upper() for k, v in pairs if k and k.strip() and v and v.strip()}

    def find(self, text: str) -> list[CodeMatch]:
        """Known codes of the text, left to right, without overlap.

        A code starts at the start of a word; it ends at the end of a word or between a digit and a glued letter
        (``MI-13991`` in ``MI-13991Table``).

        :param text: question or passage
        :return: the codes found, with their position in the text and the key written for them
        """
        text = text or ''
        pieces = [(m.start(), m.end()) for m in _PIECE.finditer(text)]
        found: list[CodeMatch] = []
        i = 0
        while i < len(pieces):
            start = pieces[i][0]
            if start > 0 and text[start - 1].isalnum():
                i += 1
                continue
            best = None
            key = ''
            for j in range(i, min(i + _MAX_PIECES, len(pieces))):
                if j > i and not _GAP.fullmatch(text, pieces[j - 1][1], pieces[j][0]):
                    break
                key += text[pieces[j][0]:pieces[j][1]].upper()
                end = pieces[j][1]
                at_word_end = end == len(text) or not text[end].isalnum()
                digit_to_letter = not at_word_end and text[end - 1].isdigit() and text[end].isalpha()
                if (at_word_end or digit_to_letter) and key in self.keys:
                    best = (j, end, self.keys[key])
            if best:
                j, end, k = best
                found.append(CodeMatch(start, end, k))
                i = j + 1
            else:
                i += 1
        return found

    def annotate(self, text: str) -> str:
        """The text with the key added after each code not already written as its key.

        ``SF-1027`` becomes ``SF-1027 [SF1027]``, ``MI-13991Table`` becomes ``MI-13991Table [MI13991]``, ``SF1027``
        stays as it is. Idempotent: a code already followed by its key gets no second one.

        :param text: question or passage
        :return: the annotated text
        """
        text = text or ''
        out, last = [], 0
        for m in self.find(text):
            written = text[m.start:m.end]
            end = _word_end(text, m.end)
            if written.upper() == m.key and end == m.end:
                continue
            tag = f' [{m.key}]'
            if text.startswith(tag, end) or end < last:
                continue
            out.append(text[last:end])
            out.append(tag)
            last = end
        out.append(text[last:])
        return ''.join(out)
# --- code_keys: end ---

# COMMAND ----------

# MAGIC %md
# MAGIC ### Le dictionnaire
# MAGIC Une ligne par graphie reconnue (`cle`) avec la clé écrite pour elle (`cle_ecrite`), sa famille et la REF qui l'a donnée. Toutes les graphies d'une même REF donnent la même clé, sans suffixe de langue : `IT-243` et `LdB IT-243` donnent `LDBIT243`, `IF2.03.08_CZ` donne `IF20308`. Les REF du catalogue y sont toujours.

# COMMAND ----------

# Cellule 3 · dictionnaire : écrit dev_proj.qualibot.f3_cles_codes


def cle(col):
    """Clé collée : majuscules, lettres et chiffres seulement.

    :param col: colonne texte
    :return: colonne de la clé
    """
    return F.upper(F.regexp_replace(col, "[^A-Za-z0-9]", ""))


def sans_langue(col):
    """Clé d'une REF sans son suffixe de langue (MI-1413_FR donne MI1413).

    :param col: colonne de la REF
    :return: colonne de la clé
    """
    return cle(F.regexp_replace(F.trim(col), f"(?i)[-_. ]+({'|'.join(LANGUES)})$", ""))


# Une graphie reconnue (cle) donne une clé écrite (cle_ecrite) : toutes les graphies d'une même REF donnent la même,
# sans suffixe de langue (IT243 et LDBIT243 donnent LDBIT243, IF20308CZ donne IF20308). prio 1 = catalogue.
lignes = None
for t in CATALOGUES:
    r = spark.table(t).select("ref").where("ref IS NOT NULL")
    r = (r.select(cle("ref").alias("cle"), "ref").unionByName(r.select(sans_langue("ref").alias("cle"), "ref"))
         .select("cle", sans_langue("ref").alias("cle_ecrite"), F.lit("ref_catalogue").alias("famille"), "ref",
                 F.lit(0).cast("long").alias("docs"), F.lit(0).cast("long").alias("q_docs"), F.lit(1).alias("prio")))
    lignes = r if lignes is None else lignes.unionByName(r)
f = spark.table(FAMILLES).where("action = 'annoter'")
par_ref = F.col("famille").isin("ref_catalogue", "ref_sans_premier_mot", "ref_sans_indice", "ref_variante")
fam = f.select(F.coalesce("cle_base", "cle").alias("cle"),
               F.when(par_ref & F.col("ref").isNotNull(), sans_langue("ref"))
               .otherwise(F.coalesce("cle_base", "cle")).alias("cle_ecrite"),
               "famille", "ref", "docs", "q_docs", F.lit(2).alias("prio"))
w = Window.partitionBy("cle").orderBy("prio", F.desc("docs"), "famille")
dico = (lignes.unionByName(fam).where(F.length("cle") > 0)
        .withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn", "prio")
        .withColumn("date_run", F.current_timestamp()))
dico.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET)
dico = spark.table(TARGET)
display(dico.groupBy("famille").agg(F.count("*").alias("cles"), F.sum("docs").alias("docs_cumules"))
        .orderBy(F.desc("cles")))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Application aux passages DEV
# MAGIC Chaque passage de `chunks` passe par le mécanisme. Une ligne par code annoté : la clé, comment il est écrit, et l'extrait avant / après. Rien n'est écrit.

# COMMAND ----------

# Cellule 4 · annotation des passages (lecture seule)

keys_b = spark.sparkContext.broadcast({r["cle"]: r["cle_ecrite"] for r in dico.select("cle", "cle_ecrite").collect()})
famille_de = dico.groupBy(F.col("cle_ecrite").alias("cle")).agg(F.min("famille").alias("famille"))
SCHEMA = "id string, cle string, ecrit string, avant string, apres string"


def annoter(batches):
    """Codes annotés de chaque texte, avec l'extrait avant et après annotation.

    :param batches: lots pandas aux colonnes id et txt
    :return: lots pandas aux colonnes de SCHEMA
    """
    ck = CodeKeys(keys_b.value)
    for pdf in batches:
        rows = []
        for i, txt in zip(pdf["id"], pdf["txt"]):
            txt = txt or ""
            annote = ck.annotate(txt)
            if annote == txt:
                continue
            for m in ck.find(txt):
                avant = txt[max(0, m.start - 60):m.end + 40]
                apres = ck.annotate(avant)
                if apres != avant:
                    rows.append((i, m.key, txt[m.start:m.end], avant, apres))
        yield pd.DataFrame(rows, columns=["id", "cle", "ecrit", "avant", "apres"])


chunks = spark.table(CHUNKS).select(F.col("chunk_id").cast("string").alias("id"), F.col("chunk_text").alias("txt"))
hits = chunks.mapInPandas(annoter, SCHEMA).join(famille_de, "cle", "left").cache()
total = chunks.count()
touches = hits.select("id").distinct().count()
print(f"passages DEV : {total} · annotés : {touches} ({100 * touches / max(total, 1):.1f} %)")
display(hits.groupBy("famille").agg(F.countDistinct("id").alias("passages"), F.countDistinct("cle").alias("cles"),
                                    F.count("*").alias("annotations"))
        .orderBy(F.desc("passages")))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Exemples avant / après
# MAGIC Cinq exemples par famille (`EXEMPLES`), les clés les plus citées d'abord.

# COMMAND ----------

# Cellule 5 · exemples avant / après par famille

par_cle = hits.groupBy("famille", "cle").agg(F.count("*").alias("n"), F.first("ecrit").alias("ecrit"),
                                              F.first("avant").alias("avant"), F.first("apres").alias("apres"))
w = Window.partitionBy("famille").orderBy(F.desc("n"))
display(par_cle.withColumn("rang", F.row_number().over(w)).where(F.col("rang") <= EXEMPLES)
        .orderBy("famille", "rang").select("famille", "cle", "ecrit", "n", "avant", "apres"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Questions du chat UAT
# MAGIC Toutes les questions où une clé est ajoutée, avant / après : c'est ce que recevra la recherche par mot clé. Rien n'est écrit.

# COMMAND ----------

# Cellule 6 · annotation des questions du chat UAT (lecture seule)

questions = (spark.table(QUESTIONS).where(F.col("question").isNotNull())
             .select(F.col("id").cast("string").alias("id"),
                     F.regexp_replace("question", r"\[Division:[^\]]*\]", " ").alias("txt")))
q_hits = questions.mapInPandas(annoter, SCHEMA).join(famille_de, "cle", "left")
display(q_hits.select("famille", "cle", "ecrit", "apres").orderBy("famille", "cle"))
