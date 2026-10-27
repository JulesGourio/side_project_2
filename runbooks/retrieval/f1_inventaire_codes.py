# Databricks notebook source
# F1 · Inventaire des acronymes et codes (étape 1 de runbooks/retrieval/README.md)
# LECTURE SEULE : aucune écriture. Droits : ton compte suffit (lecture sur l'environnement), pas de job runner.
# Corpus = tous les documents parsés (parsing_checkpoint, une ligne par IDDOC, SUCCESS en priorité), sans scope.
# Deux passes, même notebook :
#   1. ENV = "dev" : cellules 1 à 5 (corpus + référentiel REF)
#   2. ENV = "uat" : cellules 1, 2, 3, 6 (questions réelles du chatbot UAT, vs graphies du corpus UAT)
# Mode d'emploi : chaque bloc entre "# COMMAND ----------" est une cellule, dans l'ordre.
# Résultat à me renvoyer : le texte imprimé par chaque passe (copier-coller).

# COMMAND ----------
# Cellule 1 · paramètres (en dur) : mettre ENV = "dev" pour la passe 1, puis ENV = "uat" pour la passe 2

from pyspark.sql import Window
from pyspark.sql import functions as F

ENV = "dev"   # "dev" ou "uat"
CHECKPOINT = f"{ENV}_proj.qualibot.parsing_checkpoint"
CATALOG = f"{ENV}_proj.qualibot.doc_catalog"
CHAT_TURNS = f"{ENV}_proj.qualibot.chat_turns"
TOP_N = 200   # lignes affichées par tableau
MIN_OCC = 2   # une forme vue une seule fois dans le corpus n'est pas inventoriée

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

# Mots courants en majuscules : faux positifs probables parmi les sigles.
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


def src_corpus():
    """Un texte par IDDOC : la ligne SUCCESS la plus récente, sinon la plus récente."""
    df = spark.table(CHECKPOINT).where(F.col("document_text").isNotNull())
    rank = F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1)
    w = Window.partitionBy("IDDOC").orderBy(rank, F.desc("ingestion_timestamp"))
    return (df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1")
            .select(F.col("IDDOC").cast("string").alias("doc"), F.col("document_text").alias("txt")))


def src_questions():
    """Questions réelles tapées dans le chat de cet environnement."""
    df = spark.table(CHAT_TURNS).where(F.col("question").isNotNull())
    return df.select(
        F.concat(F.lit("turn#"), F.col("id").cast("string")).alias("doc"),
        F.regexp_replace(F.col("question"), r"\[Division:[^\]]*\]", " ").alias("txt"))

# COMMAND ----------
# Cellule 4 · section A : référentiel REF (doc_catalog) et leurs graphies dans le corpus. Passe ENV = "dev".

corpus = src_corpus().cache()
print(f"[{ENV}] documents parsés : {corpus.count()}")

cat = spark.table(CATALOG).select("ref").where("ref IS NOT NULL").cache()
print(f"\n=== A. Catalogue : {cat.count()} REF ===")
pref = (cat.select(F.regexp_extract("ref", r"^([A-Za-z]+)", 1).alias("prefixe"))
        .groupBy("prefixe").count().orderBy(F.desc("count")).collect())
print("préfixes :", "  ".join(f"{r['prefixe']}={r['count']}" for r in pref))
print("exemples :", " | ".join(r["ref"] for r in cat.limit(15).collect()))

refs_k = cat.withColumn("cle", F.upper(F.regexp_replace("ref", "[^A-Za-z0-9]", "")))
codes_tok = tokens(corpus, PATTERNS["code_chiffres"]).unionByName(tokens(corpus, PATTERNS["reference_segments"]))
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
# Cellule 5 · section B : tous les candidats du corpus, par catégorie. Passe ENV = "dev".

for label, pattern in PATTERNS.items():
    rows = agg(tokens(corpus, pattern)).orderBy(F.desc("occ")).limit(TOP_N).collect()
    print(f"\n=== B. {label} : top {len(rows)} (occ >= {MIN_OCC}) ===")
    for r in rows:
        print(f"{r['cle']}\t{r['occ']}\t{r['docs']}\t{len(r['formes'])} formes\t{' | '.join(r['formes'][:4])}")
    if label == "sigle":
        fp = [r["cle"] for r in rows if r["cle"] in STOPLIST]
        print(f"\nfaux positifs probables (mots courants) dans ce top : {len(fp)} → {' '.join(fp)}")

# COMMAND ----------
# Cellule 6 · section C : codes tapés dans le chat (chat_turns) vs graphie du corpus. Passe ENV = "uat".
# Lancer UNIQUEMENT dans un notebook UAT (ENV = "uat" en cellule 1). Cellules 1, 2, 3 puis 6 ; corpus lu ici aussi.

corpus = src_corpus().cache()
qsrc = src_questions().cache()
print(f"[{ENV}] questions du chat : {qsrc.count()} · documents parsés : {corpus.count()}")

for label in ["code_chiffres", "reference_segments", "sigle"]:
    qa = agg(tokens(qsrc, PATTERNS[label]), min_occ=1)
    ca = (agg(tokens(corpus, PATTERNS[label]), min_occ=1)
          .select("cle", F.col("occ").alias("occ_corpus"), F.col("formes").alias("formes_corpus")))
    rows = qa.join(ca, "cle", "left").orderBy(F.desc("occ")).limit(TOP_N).collect()
    print(f"\n=== C. {label} : top {len(rows)} (cle | questions | formes tapées | formes dans le corpus) ===")
    for r in rows:
        fc = " | ".join(r["formes_corpus"][:3]) if r["formes_corpus"] else "ABSENT du corpus"
        print(f"{r['cle']}\t{r['occ']}\t{r['docs']}\t{' | '.join(r['formes'][:3])}\t{fc}")
