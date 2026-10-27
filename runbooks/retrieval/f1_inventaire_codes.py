# Databricks notebook source
# F1 · Inventaire des acronymes et codes : une table par environnement, <ENV>_proj.qualibot.f1_inventaire_codes
# Une passe par environnement : ENV = "dev" dans le notebook DEV, ENV = "uat" dans le notebook UAT.
# Lecture seule sur <ENV>_proj.qualibot (corpus parsé, référentiel REF, questions du chat) ; la table est
# remplacée à chaque passe. Droits : ton compte (lecture et création de table dans <ENV>_proj.qualibot), pas de job runner.
# Sections : A_reference (REF du catalogue et leurs graphies), B_corpus (candidats du corpus),
# C_questions (codes tapés dans le chat, avec leur graphie dans le corpus).

# COMMAND ----------
# Cellule 1 · paramètres (en dur) et motifs

from functools import reduce

from pyspark.sql import DataFrame
from pyspark.sql import Window
from pyspark.sql import functions as F

ENV = "dev"   # "dev" dans le notebook DEV, "uat" dans le notebook UAT
assert ENV in ("dev", "uat")
TARGET = f"{ENV}_proj.qualibot.f1_inventaire_codes"
CHECKPOINT = f"{ENV}_proj.qualibot.parsing_checkpoint"
CATALOG = f"{ENV}_proj.qualibot.doc_catalog"
CHAT_TURNS = f"{ENV}_proj.qualibot.chat_turns"
MIN_OCC = 2   # occurrences minimum dans le corpus pour une clé de la section B

PATTERNS = {
    # codes à chiffres, casse ignorée : ST-1027, st1027, SF_1027, AS9100, DO-160
    "code_chiffres": r"(?i)(?<![a-z0-9])[a-z]{2,8}[-_]?[0-9]{2,}(?:[-_/.][0-9]+)*[a-z]?(?![a-z0-9])",
    # références à segments : IAQ 20 00 04, IAQ-20-00-04, ISO 9001, ST 1027-01
    "reference_segments": r"(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?: [0-9]{1,4})+(?![A-Za-z0-9])"
                          r"|(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?:[ ./-][0-9]{1,4}){2,}(?![A-Za-z0-9])",
    # sigles en majuscules sans chiffre : DOA, AIDS, NADCAP
    "sigle": r"(?<![A-Za-z0-9])[A-Z]{2,8}(?![A-Za-z0-9])",
    # numérotations pointées : 4.10.2 (surtout des numéros de chapitre)
    "numero_pointe": r"(?<![0-9.])[0-9]{1,3}(?:\.[0-9]{1,3}){2,}(?![0-9.])",
}

# Mots courants en majuscules : faux positifs probables parmi les sigles (colonne categorie = sigle, à filtrer).
STOPLIST = {"OK", "NON", "OUI", "ET", "OU", "DE", "LE", "LA", "LES", "UN", "UNE", "DU", "DES", "EN", "AU", "AUX",
            "SI", "CE", "QUE", "QUI", "EST", "PAR", "POUR", "SUR", "DANS", "AND", "THE", "FOR", "NOT", "ALL", "ANY",
            "AT", "BY", "IN", "IS", "OF", "OR", "TO", "NO", "YES", "PDF", "NA", "RAS", "TBD", "ID", "FR"}

# COMMAND ----------
# Cellule 2 · fonctions (aucune écriture)


def tokens(src, pattern):
    """Une ligne par occurrence du motif dans la colonne txt, avec la source doc.

    :param src: DataFrame aux colonnes doc et txt
    :param pattern: expression régulière (syntaxe Java)
    :return: DataFrame aux colonnes doc et tok
    """
    pat = pattern.replace("\\", "\\\\")
    return src.select("doc", F.explode(F.expr(f"regexp_extract_all(txt, '{pat}', 0)")).alias("tok"))


def agg(tok_df, min_occ=MIN_OCC):
    """Une ligne par clé normalisée (majuscules, sans tiret, espace, point ni slash).

    :param tok_df: DataFrame aux colonnes doc et tok
    :param min_occ: occurrences minimum pour garder la clé
    :return: DataFrame aux colonnes cle, occ, docs et formes (graphies vues, triées)
    """
    return (tok_df.withColumn("cle", F.upper(F.regexp_replace("tok", "[^A-Za-z0-9]", "")))
            .groupBy("cle")
            .agg(F.count("*").alias("occ"), F.countDistinct("doc").alias("docs"),
                 F.array_sort(F.collect_set("tok")).alias("formes"))
            .where(F.col("occ") >= min_occ))


def shape(df, section, categorie):
    """Ramène un DataFrame de clés aux colonnes de la table.

    :param df: DataFrame aux colonnes cle, ref_catalogue, occ, docs, formes, occ_corpus, formes_corpus
    :param section: nom de la section (A_reference, B_corpus, C_questions)
    :param categorie: nom du motif ou de la catégorie de référence
    :return: DataFrame aux colonnes de la table
    """
    return df.select(
        F.lit(section).alias("section"), F.lit(categorie).alias("categorie"), "cle", "ref_catalogue",
        F.col("occ").cast("long"), F.col("docs").cast("long"), "formes",
        F.col("occ_corpus").cast("long"), "formes_corpus",
        F.current_timestamp().alias("date_run"))


def src_corpus():
    """Un texte par IDDOC : la ligne SUCCESS la plus récente, sinon la plus récente.

    :return: DataFrame aux colonnes doc et txt
    """
    df = spark.table(CHECKPOINT).where(F.col("document_text").isNotNull())
    rank = F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1)
    w = Window.partitionBy("IDDOC").orderBy(rank, F.desc("ingestion_timestamp"))
    return (df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1")
            .select(F.col("IDDOC").cast("string").alias("doc"), F.col("document_text").alias("txt")))


def src_questions():
    """Questions tapées dans le chat de cet environnement, sans le préfixe de division.

    :return: DataFrame aux colonnes doc et txt, vide si la table n'existe pas
    """
    try:
        df = spark.table(CHAT_TURNS).where(F.col("question").isNotNull())
    except Exception as e:
        print(f"ABSENT : {CHAT_TURNS} ({str(e)[:120]})")
        return spark.createDataFrame([], "doc string, txt string")
    return df.select(
        F.concat(F.lit("turn#"), F.col("id").cast("string")).alias("doc"),
        F.regexp_replace(F.col("question"), r"\[Division:[^\]]*\]", " ").alias("txt"))

# COMMAND ----------
# Cellule 3 · construction et écriture de la table (remplace la table de cet environnement)

corpus = src_corpus().cache()
qsrc = src_questions().cache()
print(f"[{ENV}] documents parsés : {corpus.count()} · questions du chat : {qsrc.count()}")

frames = []

# Section A : chaque REF du catalogue, citations exactes (occ) et sous une autre graphie (occ_corpus)
cat = spark.table(CATALOG).select("ref").where("ref IS NOT NULL").cache()
refs_k = cat.withColumn("cle", F.upper(F.regexp_replace("ref", "[^A-Za-z0-9]", "")))
codes_tok = tokens(corpus, PATTERNS["code_chiffres"]).unionByName(tokens(corpus, PATTERNS["reference_segments"]))
hits = (codes_tok.withColumn("cle", F.upper(F.regexp_replace("tok", "[^A-Za-z0-9]", "")))
        .join(F.broadcast(refs_k), "cle")
        .groupBy("ref")
        .agg(F.sum(F.when(F.col("tok") == F.col("ref"), 1).otherwise(0)).alias("exact"),
             F.sum(F.when(F.col("tok") != F.col("ref"), 1).otherwise(0)).alias("variante"),
             F.array_sort(F.collect_set(F.when(F.col("tok") != F.col("ref"), F.col("tok")))).alias("formes_variantes")))
a = (cat.join(hits, "ref", "left")
     .select(F.col("ref").alias("cle"), F.col("ref").alias("ref_catalogue"),
             F.coalesce("exact", F.lit(0)).alias("occ"), F.lit(None).cast("long").alias("docs"),
             F.coalesce("formes_variantes", F.array().cast("array<string>")).alias("formes"),
             F.coalesce("variante", F.lit(0)).alias("occ_corpus"),
             F.lit(None).cast("array<string>").alias("formes_corpus")))
frames.append(shape(a, "A_reference", "reference"))

# Section B : candidats du corpus, par motif
for label, pattern in PATTERNS.items():
    b = agg(tokens(corpus, pattern)).select(
        "cle", F.lit(None).cast("string").alias("ref_catalogue"), "occ", "docs", "formes",
        F.lit(None).cast("long").alias("occ_corpus"), F.lit(None).cast("array<string>").alias("formes_corpus"))
    frames.append(shape(b, "B_corpus", label))

# Section C : codes tapés dans le chat (occ = questions) et leur graphie dans le corpus (occ_corpus)
for label in ["code_chiffres", "reference_segments", "sigle"]:
    qa = agg(tokens(qsrc, PATTERNS[label]), min_occ=1)
    ca = agg(tokens(corpus, PATTERNS[label]), min_occ=1).select(
        "cle", F.col("occ").alias("occ_corpus"), F.col("formes").alias("formes_corpus"))
    c = qa.join(ca, "cle", "left").select(
        "cle", F.lit(None).cast("string").alias("ref_catalogue"), "occ", "docs", "formes",
        "occ_corpus", "formes_corpus")
    frames.append(shape(c, "C_questions", label))

out = reduce(DataFrame.unionByName, frames)
out.write.mode("overwrite").saveAsTable(TARGET)
print(f"écrit : {TARGET}")

# COMMAND ----------
# Cellule 4 · contrôle : nombre de lignes par section et catégorie

for r in (spark.table(TARGET).groupBy("section", "categorie").count()
          .orderBy("section", "categorie").collect()):
    print(f"{r['section']}\t{r['categorie']}\t{r['count']}")
