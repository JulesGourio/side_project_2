# Databricks notebook source
# Inventaire des acronymes et codes : tokens du corpus parsé, référentiel REF et questions du chat,
# écrits dans dev_proj.qualibot.f1_inventaire_codes (colonne env).
# Lecture seule sur <env>_proj.qualibot pour chaque env de ENVS ; seule la table cible est écrite.
#
# Sections :
#   A_reference : une ligne par REF du catalogue. occ = citations exactes de la REF dans le corpus,
#                 occ_autre_graphie = citations de la même REF écrites autrement (espace, tiret, casse).
#   B_corpus    : une ligne par clé (code, segments, sigle, numéro pointé) présente dans le corpus.
#                 occ = occurrences, docs = documents qui la contiennent.
#   C_questions : une ligne par clé tapée dans le chat. occ = questions qui la contiennent, docs = questions,
#                 occ_corpus = occurrences de la même clé dans le corpus, formes_corpus = leurs graphies.
#
# Colonnes communes :
#   formes             : graphies exactes vues dans la source de la ligne (triées).
#   nb_refs            : nombre de REF distinctes du corpus qui citent la clé (sections B et C).
#   refs_source        : les 10 REF qui citent le plus la clé, avec le nombre de documents, "REF (n)".
#   urls_source        : lien Intraqual de chacune de ces REF.

# COMMAND ----------
# Cellule 1 · paramètres et motifs

from functools import reduce

from pyspark.sql import DataFrame
from pyspark.sql import Window
from pyspark.sql import functions as F

ENVS = ["dev", "uat"]   # environnements à inventorier : ["dev", "uat"], ["dev"] ou ["uat"]
TARGET = "dev_proj.qualibot.f1_inventaire_codes"
MIN_OCC = 2   # occurrences minimum dans le corpus pour une clé de la section B
INTRAQUAL_BASE = "https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref="

# Motifs appliqués au texte des documents et des questions, en casse exacte sauf indication contraire.
PATTERNS = {
    # Code à chiffres : 2 à 8 lettres, tiret ou underscore facultatif, au moins 2 chiffres, puis des segments
    # numériques facultatifs séparés par - _ / . et une lettre finale facultative. Casse ignorée.
    # Matche ST-1027, st1027, SF_1027, AS9100, DO-160, QR-1005-02, ST1027a.
    # Ne matche pas une date (pas de lettres) ni un mot suivi d'un nombre séparé par un espace.
    "code_chiffres": r"(?i)(?<![a-z0-9])[a-z]{2,8}[-_]?[0-9]{2,}(?:[-_/.][0-9]+)*[a-z]?(?![a-z0-9])",
    # Référence à segments, majuscules seulement. Deux formes :
    #   - des lettres (2 ou plus) suivies de segments numériques séparés par un espace : IAQ 20 00 04, ISO 9001 ;
    #   - des lettres suivies d'au moins deux segments séparés par espace, point, slash ou tiret : ST-1027-01, 12/34/56.
    # Ne matche pas ST-1027 seul (un seul segment : couvert par code_chiffres).
    "reference_segments": r"(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?: [0-9]{1,4})+(?![A-Za-z0-9])"
                          r"|(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?:[ ./-][0-9]{1,4}){2,}(?![A-Za-z0-9])",
    # Sigle : 2 à 8 majuscules, sans chiffre ni lettre collée. Matche DOA, AIDS, NADCAP, mais aussi des mots
    # en capitales (OK, NON) : ces faux positifs sont écartés à la lecture des graphiques.
    "sigle": r"(?<![A-Za-z0-9])[A-Z]{2,8}(?![A-Za-z0-9])",
    # Numéro pointé : 3 blocs de 1 à 3 chiffres séparés par des points : 4.10.2.
    # Matche aussi des adresses IP et des numéros de version à trois niveaux.
    "numero_pointe": r"(?<![0-9.])[0-9]{1,3}(?:\.[0-9]{1,3}){2,}(?![0-9.])",
}

TYPES = {
    "cle": "string", "ref_catalogue": "string", "occ": "long", "docs": "long",
    "formes": "array<string>", "occ_corpus": "long", "formes_corpus": "array<string>",
    "occ_autre_graphie": "long", "formes_autre_graphie": "array<string>",
    "nb_refs": "long", "refs_source": "array<string>", "urls_source": "array<string>",
}

# COMMAND ----------
# Cellule 2 · fonctions (aucune écriture)


def with_cle(tok_df):
    """Ajoute la clé normalisée : majuscules, sans tiret, espace, point ni slash.

    :param tok_df: DataFrame aux colonnes doc, doc_ref et tok
    :return: même DataFrame avec la colonne cle
    """
    return tok_df.withColumn("cle", F.upper(F.regexp_replace("tok", "[^A-Za-z0-9]", "")))


def tokens(src, pattern):
    """Une ligne par occurrence du motif dans la colonne txt.

    :param src: DataFrame aux colonnes doc, doc_ref et txt
    :param pattern: expression régulière (syntaxe Java)
    :return: DataFrame aux colonnes doc, doc_ref et tok
    """
    pat = pattern.replace("\\", "\\\\")
    return src.select("doc", "doc_ref",
                      F.explode(F.expr(f"regexp_extract_all(txt, '{pat}', 0)")).alias("tok"))


def keyed(tok_df, min_occ=MIN_OCC):
    """Statistiques par clé, avec les REF qui la citent et leurs liens Intraqual.

    :param tok_df: DataFrame aux colonnes doc, doc_ref et tok
    :param min_occ: occurrences minimum pour garder la clé
    :return: DataFrame aux colonnes cle, occ, docs, formes, nb_refs, refs_source, urls_source
    """
    t = with_cle(tok_df)
    stats = (t.groupBy("cle")
             .agg(F.count("*").alias("occ"), F.countDistinct("doc").alias("docs"),
                  F.array_sort(F.collect_set("tok")).alias("formes"))
             .where(F.col("occ") >= min_occ))
    per_ref = (t.where(F.col("doc_ref").isNotNull())
               .groupBy("cle", "doc_ref").agg(F.countDistinct("doc").alias("n"))
               .groupBy("cle")
               .agg(F.sort_array(F.collect_list(F.struct("n", "doc_ref")), asc=False).alias("s"),
                    F.count("doc_ref").alias("nb_refs")))
    refs = per_ref.select(
        "cle", "nb_refs",
        F.transform(F.slice("s", 1, 10),
                    lambda x: F.concat(x["doc_ref"], F.lit(" ("), x["n"].cast("string"), F.lit(")")))
        .alias("refs_source"),
        F.transform(F.slice("s", 1, 10),
                    lambda x: F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace(x["doc_ref"], " ", "%20")))
        .alias("urls_source"))
    return stats.join(refs, "cle", "left")


def conform(df, env, section, categorie):
    """Ramène un DataFrame aux colonnes de la table ; une colonne absente de la section est nulle.

    :param df: DataFrame de la section
    :param env: environnement source (dev ou uat)
    :param section: nom de la section (A_reference, B_corpus, C_questions)
    :param categorie: motif ou catégorie de référence
    :return: DataFrame aux colonnes de la table
    """
    cols = [F.col(c).cast(t).alias(c) if c in df.columns else F.lit(None).cast(t).alias(c)
            for c, t in TYPES.items()]
    return df.select(F.lit(env).alias("env"), F.lit(section).alias("section"),
                     F.lit(categorie).alias("categorie"), *cols,
                     F.current_timestamp().alias("date_run"))


def src_corpus(env):
    """Un texte par IDDOC du parsing_checkpoint : la ligne SUCCESS la plus récente, sinon la plus récente.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes doc, doc_ref et txt
    """
    df = spark.table(f"{env}_proj.qualibot.parsing_checkpoint").where(F.col("document_text").isNotNull())
    rank = F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1)
    w = Window.partitionBy("IDDOC").orderBy(rank, F.desc("ingestion_timestamp"))
    return (df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1")
            .select(F.col("IDDOC").cast("string").alias("doc"),
                    F.col("ref").cast("string").alias("doc_ref"),
                    F.col("document_text").alias("txt")))


def src_questions(env):
    """Questions tapées dans le chat de l'environnement, sans le préfixe de division.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes doc, doc_ref (nul) et txt ; vide si la table n'est pas lisible
    """
    try:
        df = spark.table(f"{env}_proj.qualibot.chat_turns").where(F.col("question").isNotNull())
    except Exception as e:
        print(f"[{env}] chat_turns illisible : {str(e)[:160]}")
        return spark.createDataFrame([], "doc string, doc_ref string, txt string")
    return df.select(
        F.concat(F.lit("turn#"), F.col("id").cast("string")).alias("doc"),
        F.lit(None).cast("string").alias("doc_ref"),
        F.regexp_replace(F.col("question"), r"\[Division:[^\]]*\]", " ").alias("txt"))


def build(env):
    """Construit les lignes des trois sections pour un environnement.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes de la table
    """
    corpus = src_corpus(env).cache()
    qsrc = src_questions(env).cache()
    print(f"[{env}] documents parsés : {corpus.count()} · questions du chat : {qsrc.count()}")
    frames = []

    # Section A : chaque REF du catalogue, citations exactes et citations sous une autre graphie
    cat = spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL")
    refs_k = cat.withColumn("cle", F.upper(F.regexp_replace("ref", "[^A-Za-z0-9]", "")))
    codes_tok = tokens(corpus, PATTERNS["code_chiffres"]).unionByName(tokens(corpus, PATTERNS["reference_segments"]))
    hits = (with_cle(codes_tok)
            .join(F.broadcast(refs_k), "cle")
            .groupBy("ref")
            .agg(F.sum(F.when(F.col("tok") == F.col("ref"), 1).otherwise(0)).alias("exact"),
                 F.sum(F.when(F.col("tok") != F.col("ref"), 1).otherwise(0)).alias("occ_autre_graphie"),
                 F.array_sort(F.collect_set(F.when(F.col("tok") != F.col("ref"), F.col("tok"))))
                 .alias("formes_autre_graphie")))
    a = (cat.join(hits, "ref", "left")
         .select(F.col("ref").alias("cle"), F.col("ref").alias("ref_catalogue"),
                 F.coalesce("exact", F.lit(0)).alias("occ"),
                 F.coalesce("occ_autre_graphie", F.lit(0)).alias("occ_autre_graphie"),
                 F.coalesce("formes_autre_graphie", F.array().cast("array<string>")).alias("formes_autre_graphie"),
                 F.lit(1).alias("nb_refs"),
                 F.array(F.col("ref")).alias("refs_source"),
                 F.array(F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace("ref", " ", "%20"))).alias("urls_source")))
    frames.append(conform(a, env, "A_reference", "reference"))

    # Section B : candidats du corpus, par motif
    for label, pattern in PATTERNS.items():
        b = keyed(tokens(corpus, pattern))
        frames.append(conform(b, env, "B_corpus", label))

    # Section C : codes des questions, avec leur présence dans le corpus et les REF qui les citent
    for label in ["code_chiffres", "reference_segments", "sigle"]:
        qa = keyed(tokens(qsrc, PATTERNS[label]), min_occ=1).select("cle", "occ", "docs", "formes")
        ca = keyed(tokens(corpus, PATTERNS[label]), min_occ=1).select(
            "cle", F.col("occ").alias("occ_corpus"), F.col("formes").alias("formes_corpus"),
            "nb_refs", "refs_source", "urls_source")
        c = qa.join(ca, "cle", "left")
        frames.append(conform(c, env, "C_questions", label))

    return reduce(DataFrame.unionByName, frames)

# COMMAND ----------
# Cellule 3 · écriture : remplace les lignes des environnements de ENVS dans la table

parts = [build(env) for env in ENVS]
out = reduce(DataFrame.unionByName, parts)
if spark.catalog.tableExists(TARGET):
    for env in ENVS:
        spark.sql(f"DELETE FROM {TARGET} WHERE env = '{env}'")
    out.write.mode("append").saveAsTable(TARGET)
else:
    out.write.mode("overwrite").saveAsTable(TARGET)
print(f"écrit dans {TARGET} : {ENVS}")

# COMMAND ----------
# Cellule 4 · contrôle : nombre de lignes par environnement, section et catégorie

for r in (spark.table(TARGET).groupBy("env", "section", "categorie").count()
          .orderBy("env", "section", "categorie").collect()):
    print(f"{r['env']}\t{r['section']}\t{r['categorie']}\t{r['count']}")

# COMMAND ----------
# Cellule 5 · graphiques à partir de la table (lecture seule)

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Patch

PALETTE = {"dev": "#1f5aa6", "uat": "#d9822b"}   # bleu et orange : paire distinguable en daltonisme
INK, MUTED, GRID = "#1f2933", "#6b7280", "#e5e7eb"
TOP = 20
STOPLIST = {"OK", "NON", "OUI", "ET", "OU", "DE", "LE", "LA", "LES", "UN", "UNE", "DU", "DES", "EN", "AU", "AUX",
            "SI", "CE", "QUE", "QUI", "EST", "PAR", "POUR", "SUR", "DANS", "AND", "THE", "FOR", "NOT", "ALL", "ANY",
            "AT", "BY", "IN", "IS", "OF", "OR", "TO", "NO", "YES", "PDF", "NA", "RAS", "TBD", "ID", "FR"}

inv = spark.table(TARGET).toPandas()
ENVS_PRESENT = [e for e in ["dev", "uat"] if e in set(inv["env"])]


def style(ax, title, xlabel):
    """Met un axe en style sobre : titre à gauche, grille horizontale légère, sans cadre.

    :param ax: axe matplotlib
    :param title: titre du graphique
    :param xlabel: libellé de l'axe horizontal
    :return: None
    """
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color=INK, pad=12)
    ax.set_xlabel(xlabel, color=MUTED, fontsize=10)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK, length=0, labelsize=9)


def top_rows(section, categorie, env, value="occ"):
    """Les TOP lignes d'une section, catégorie et environnement, triées par valeur décroissante.

    :param section: nom de la section
    :param categorie: nom de la catégorie
    :param env: environnement (dev ou uat)
    :param value: colonne de tri
    :return: DataFrame des lignes retenues, plus grande valeur en premier
    """
    rows = inv[(inv.section == section) & (inv.categorie == categorie) & (inv.env == env)]
    return rows.sort_values(value, ascending=False).head(TOP)


def panels(n_env):
    """Crée une figure avec un panneau par environnement.

    :param n_env: nombre d'environnements à afficher
    :return: figure et liste d'axes
    """
    fig, axes = plt.subplots(1, n_env, figsize=(7 * n_env, 7), squeeze=False)
    return fig, list(axes[0])


# Vue d'ensemble : lignes par section et environnement
counts = inv.groupby(["section", "env"]).size().unstack("env").reindex(columns=ENVS_PRESENT).fillna(0)
fig, ax = plt.subplots(figsize=(10, 4.5))
x = range(len(counts.index))
width = 0.38
for i, env in enumerate(ENVS_PRESENT):
    offset = (i - (len(ENVS_PRESENT) - 1) / 2) * width
    ax.bar([p + offset for p in x], counts[env], width=width * 0.92, color=PALETTE[env], label=env.upper())
    for p, v in zip(x, counts[env]):
        ax.text(p + offset, v, f"{int(v):,}", ha="center", va="bottom", fontsize=8, color=MUTED)
ax.set_xticks(list(x))
ax.set_xticklabels(counts.index, color=INK)
style(ax, "Lignes de l'inventaire par section", "")
ax.xaxis.grid(False)
ax.yaxis.grid(True, color=GRID, linewidth=0.8)
ax.set_ylabel("lignes", color=MUTED, fontsize=10)
ax.legend(frameon=False, fontsize=9)
fig.tight_layout()
plt.show()

# Section A : REF du catalogue, citations exactes et citations sous une autre graphie
fig, axes = panels(len(ENVS_PRESENT))
for ax, env in zip(axes, ENVS_PRESENT):
    d = top_rows("A_reference", "reference", env, "occ_autre_graphie").iloc[::-1]
    ax.barh(d.cle, d.occ, color=PALETTE[env], height=0.7, edgecolor="white", linewidth=0.5)
    ax.barh(d.cle, d.occ_autre_graphie, left=d.occ, color=PALETTE[env], alpha=0.35, height=0.7,
            edgecolor="white", linewidth=0.5)
    ax.legend(handles=[Patch(color=PALETTE[env], label="citation exacte"),
                       Patch(color=PALETTE[env], alpha=0.35, label="autre graphie")],
              frameon=False, fontsize=9, loc="lower right")
    style(ax, f"{env.upper()} · REF citées sous une autre graphie", "nombre de citations")
fig.tight_layout()
plt.show()

# Section B : candidats du corpus, par motif
for categorie in ["code_chiffres", "reference_segments", "sigle"]:
    fig, axes = panels(len(ENVS_PRESENT))
    for ax, env in zip(axes, ENVS_PRESENT):
        d = top_rows("B_corpus", categorie, env)
        if categorie == "sigle":
            d = d[~d.cle.isin(STOPLIST)]
        d = d.iloc[::-1]
        ax.barh(d.cle, d.occ, color=PALETTE[env], height=0.7, edgecolor="white", linewidth=0.5)
        for y, v in enumerate(d.occ):
            ax.text(v, y, f" {int(v):,}", va="center", fontsize=8, color=MUTED)
        style(ax, f"{env.upper()} · {categorie}", "occurrences dans le corpus")
    fig.tight_layout()
    plt.show()

# Section C : codes tapés dans le chat, en gris quand ils sont absents du corpus
fig, axes = panels(len(ENVS_PRESENT))
for ax, env in zip(axes, ENVS_PRESENT):
    rows = inv[(inv.section == "C_questions") & (inv.env == env)]
    d = rows.sort_values("occ", ascending=False).head(TOP).iloc[::-1]
    colors = [PALETTE[env] if pd.notna(v) else MUTED for v in d.occ_corpus]
    ax.barh(d.cle, d.occ, color=colors, height=0.7, edgecolor="white", linewidth=0.5)
    ax.legend(handles=[Patch(color=PALETTE[env], label="présent dans le corpus"),
                       Patch(color=MUTED, label="absent du corpus")],
              frameon=False, fontsize=9, loc="lower right")
    style(ax, f"{env.upper()} · codes tapés dans le chat", "questions")
fig.tight_layout()
plt.show()
