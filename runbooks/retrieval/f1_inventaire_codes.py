# Databricks notebook source
# F1 · Inventaire des acronymes et codes (étape 1 de runbooks/retrieval/README.md)
# LECTURE SEULE, DEV uniquement : aucune écriture, aucun accès Lakebase (on lit les copies Delta dev_proj.qualibot).
# Droits : ton compte suffit (lecture sur dev_proj.qualibot), pas besoin du job runner.
# Mode d'emploi : chaque bloc entre "# COMMAND ----------" est une cellule, à exécuter dans l'ordre.
# Résultat à me renvoyer : le texte imprimé par les cellules 4, 5 et 6 (copier-coller).

# COMMAND ----------
# Cellule 1 · paramètres (en dur)

from functools import reduce

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

CHUNKS = "dev_proj.qualibot.chunks"
CATALOG = "dev_proj.qualibot.doc_catalog"
CHAT_TURNS = "dev_proj.qualibot.chat_turns"
GOLDEN = "dev_proj.qualibot.eval_golden_questions"
FEEDBACK = "dev_proj.qualibot.eval_feedback_failures"
TOP_N = 200   # lignes affichées par tableau
MIN_OCC = 2   # une forme vue une seule fois dans les chunks n'est pas inventoriée

# COMMAND ----------
# Cellule 2 · motifs. Chaque motif est une catégorie ; on ratisse large, le tri se fait après.

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

# Mots courants en majuscules : ce sont des faux positifs probables parmi les sigles.
STOPLIST = {"OK", "NON", "OUI", "ET", "OU", "DE", "LE", "LA", "LES", "UN", "UNE", "DU", "DES", "EN", "AU", "AUX",
            "SI", "CE", "QUE", "QUI", "EST", "PAR", "POUR", "SUR", "DANS", "AND", "THE", "FOR", "NOT", "ALL", "ANY",
            "AT", "BY", "IN", "IS", "OF", "OR", "TO", "NO", "YES", "PDF", "NA", "RAS", "TBD", "ID", "FR"}

# COMMAND ----------
# Cellule 3 · fonctions (aucune écriture)


def tokens(src, pattern):
    """Une ligne par occurrence du motif dans la colonne txt, avec la source doc."""
    pat = pattern.replace("\\", "\\\\")
    return src.select("doc", F.explode(F.expr(f"regexp_extract_all(txt, '{pat}', 0)")).alias("tok"))


def agg(tok_df, min_occ=MIN_OCC):
    """Une ligne par clé normalisée (majuscules, sans tiret, espace, point ni slash)."""
    return (tok_df.withColumn("cle", F.upper(F.regexp_replace("tok", "[^A-Za-z0-9]", "")))
            .groupBy("cle")
            .agg(F.count("*").alias("occ"), F.countDistinct("doc").alias("docs"),
                 F.array_sort(F.collect_set("tok")).alias("formes"))
            .where(F.col("occ") >= min_occ))


def src_chunks():
    cols = spark.table(CHUNKS).columns
    doc_col = "ref" if "ref" in cols else "source_file_name"
    return (spark.table(CHUNKS).where(F.col("chunk_text").isNotNull())
            .select(F.col(doc_col).alias("doc"), F.col("chunk_text").alias("txt")))


def src_questions():
    parts = []
    for table, col in [(CHAT_TURNS, "question"), (GOLDEN, "inputs"), (FEEDBACK, "question")]:
        try:
            df = spark.table(table)
        except Exception as e:
            print(f"ABSENT : {table} ({str(e)[:120]})")
            continue
        parts.append(df.select(
            F.concat(F.lit(table + "#"), F.monotonically_increasing_id().cast("string")).alias("doc"),
            F.regexp_replace(F.col(col).cast("string"), r"\[Division:[^\]]*\]", " ").alias("txt")))
    if not parts:
        raise RuntimeError("aucune table de questions lisible")
    return reduce(DataFrame.unionByName, parts)

# COMMAND ----------
# Cellule 4 · section A : références officielles (doc_catalog) et leurs graphies dans les chunks

chunks = src_chunks().cache()
print(f"chunks : {chunks.count()}")

cat = spark.table(CATALOG).select("ref").where("ref IS NOT NULL").cache()
print(f"\n=== A. Catalogue : {cat.count()} REF ===")
pref = (cat.select(F.regexp_extract("ref", r"^([A-Za-z]+)", 1).alias("prefixe"))
        .groupBy("prefixe").count().orderBy(F.desc("count")).collect())
print("préfixes :", "  ".join(f"{r['prefixe']}={r['count']}" for r in pref))
print("exemples :", " | ".join(r["ref"] for r in cat.limit(15).collect()))

refs_k = cat.withColumn("cle", F.upper(F.regexp_replace("ref", "[^A-Za-z0-9]", "")))
codes_tok = tokens(chunks, PATTERNS["code_chiffres"]).unionByName(tokens(chunks, PATTERNS["reference_segments"]))
hits = (codes_tok.withColumn("cle", F.upper(F.regexp_replace("tok", "[^A-Za-z0-9]", "")))
        .join(F.broadcast(refs_k), "cle")
        .groupBy("ref")
        .agg(F.sum(F.when(F.col("tok") == F.col("ref"), 1).otherwise(0)).alias("exact"),
             F.sum(F.when(F.col("tok") != F.col("ref"), 1).otherwise(0)).alias("variante"),
             F.array_sort(F.collect_set(F.when(F.col("tok") != F.col("ref"), F.col("tok")))).alias("formes_variantes"))
        .cache())

tot = hits.agg(F.sum("exact").alias("e"), F.sum("variante").alias("v"), F.count("*").alias("n")).collect()[0]
sans = cat.join(hits.select("ref"), "ref", "left_anti").count()
print(f"citations exactes : {tot['e']} · sous une autre graphie : {tot['v']} · REF citées : {tot['n']} · "
      f"REF jamais trouvées par tokenisation : {sans}")
print("\nREF citées sous une autre graphie (ref | exact | variante | formes) :")
for r in hits.where("variante > 0").orderBy(F.desc("variante")).limit(TOP_N).collect():
    print(f"{r['ref']}\t{r['exact']}\t{r['variante']}\t{' | '.join(r['formes_variantes'][:3])}")

# COMMAND ----------
# Cellule 5 · section B : tous les candidats des chunks, par catégorie

for label, pattern in PATTERNS.items():
    rows = agg(tokens(chunks, pattern)).orderBy(F.desc("occ")).limit(TOP_N).collect()
    print(f"\n=== B. {label} : top {len(rows)} (occ >= {MIN_OCC}) ===")
    for r in rows:
        print(f"{r['cle']}\t{r['occ']}\t{r['docs']}\t{len(r['formes'])} formes\t{' | '.join(r['formes'][:4])}")
    if label == "sigle":
        fp = [r["cle"] for r in rows if r["cle"] in STOPLIST]
        print(f"\nfaux positifs probables (mots courants) dans ce top : {len(fp)} → {' '.join(fp)}")

# COMMAND ----------
# Cellule 6 · section C : codes tapés par les utilisateurs (chat, golden, feedback) vs graphie des chunks

qsrc = src_questions().cache()
print(f"\nquestions (chat + golden + feedback) : {qsrc.count()}")

for label in ["code_chiffres", "reference_segments", "sigle"]:
    qa = agg(tokens(qsrc, PATTERNS[label]), min_occ=1)
    ca = (agg(tokens(chunks, PATTERNS[label]), min_occ=1)
          .select("cle", F.col("occ").alias("occ_chunks"), F.col("formes").alias("formes_chunks")))
    rows = qa.join(ca, "cle", "left").orderBy(F.desc("occ")).limit(TOP_N).collect()
    print(f"\n=== C. {label} : top {len(rows)} (cle | questions | docs questions | formes tapées | formes dans les chunks) ===")
    for r in rows:
        fc = " | ".join(r["formes_chunks"][:3]) if r["formes_chunks"] else "ABSENT des chunks"
        print(f"{r['cle']}\t{r['occ']}\t{r['docs']}\t{' | '.join(r['formes'][:3])}\t{fc}")
