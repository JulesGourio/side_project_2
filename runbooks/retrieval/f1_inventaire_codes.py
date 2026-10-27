# Databricks notebook source
# Inventaire des acronymes et codes : tokens du corpus parsé, référentiel REF et questions du chat,
# écrits dans dev_proj.qualibot.f1_inventaire_codes (colonne env).
# Lecture seule sur <env>_proj.qualibot pour chaque env de ENVS ; seule la table cible est écrite.
# Sections : A_reference (REF du catalogue et leurs graphies), B_corpus (candidats du corpus),
# C_questions (codes tapés dans le chat, avec leur graphie dans le corpus).

# COMMAND ----------
# Cellule 1 · paramètres et motifs

from functools import reduce

from pyspark.sql import DataFrame
from pyspark.sql import Window
from pyspark.sql import functions as F

ENVS = ["dev", "uat"]   # environnements à inventorier : ["dev", "uat"], ["dev"] ou ["uat"]
TARGET = "dev_proj.qualibot.f1_inventaire_codes"
MIN_OCC = 2   # occurrences minimum dans le corpus pour une clé de la section B

PATTERNS = {
    "code_chiffres": r"(?i)(?<![a-z0-9])[a-z]{2,8}[-_]?[0-9]{2,}(?:[-_/.][0-9]+)*[a-z]?(?![a-z0-9])",
    "reference_segments": r"(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?: [0-9]{1,4})+(?![A-Za-z0-9])"
                          r"|(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?:[ ./-][0-9]{1,4}){2,}(?![A-Za-z0-9])",
    "sigle": r"(?<![A-Za-z0-9])[A-Z]{2,8}(?![A-Za-z0-9])",
    "numero_pointe": r"(?<![0-9.])[0-9]{1,3}(?:\.[0-9]{1,3}){2,}(?![0-9.])",
}

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


def shape(df, env, section, categorie):
    """Ramène un DataFrame de clés aux colonnes de la table.

    :param df: DataFrame aux colonnes cle, ref_catalogue, occ, docs, formes, occ_corpus, formes_corpus
    :param env: environnement source (dev ou uat)
    :param section: nom de la section (A_reference, B_corpus, C_questions)
    :param categorie: nom du motif ou de la catégorie de référence
    :return: DataFrame aux colonnes de la table
    """
    return df.select(
        F.lit(env).alias("env"), F.lit(section).alias("section"), F.lit(categorie).alias("categorie"),
        "cle", "ref_catalogue", F.col("occ").cast("long"), F.col("docs").cast("long"), "formes",
        F.col("occ_corpus").cast("long"), "formes_corpus", F.current_timestamp().alias("date_run"))


def src_corpus(env):
    """Un texte par IDDOC du parsing_checkpoint de l'environnement : la ligne SUCCESS la plus récente, sinon la plus récente.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes doc et txt
    """
    df = spark.table(f"{env}_proj.qualibot.parsing_checkpoint").where(F.col("document_text").isNotNull())
    rank = F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1)
    w = Window.partitionBy("IDDOC").orderBy(rank, F.desc("ingestion_timestamp"))
    return (df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1")
            .select(F.col("IDDOC").cast("string").alias("doc"), F.col("document_text").alias("txt")))


def src_questions(env):
    """Questions tapées dans le chat de l'environnement, sans le préfixe de division.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes doc et txt, vide si la table n'est pas lisible
    """
    try:
        df = spark.table(f"{env}_proj.qualibot.chat_turns").where(F.col("question").isNotNull())
    except Exception as e:
        print(f"[{env}] chat_turns illisible : {str(e)[:160]}")
        return spark.createDataFrame([], "doc string, txt string")
    return df.select(
        F.concat(F.lit("turn#"), F.col("id").cast("string")).alias("doc"),
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

    # Section A : chaque REF du catalogue, citations exactes (occ) et sous une autre graphie (occ_corpus)
    cat = spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL")
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
    frames.append(shape(a, env, "A_reference", "reference"))

    # Section B : candidats du corpus, par motif
    for label, pattern in PATTERNS.items():
        b = agg(tokens(corpus, pattern)).select(
            "cle", F.lit(None).cast("string").alias("ref_catalogue"), "occ", "docs", "formes",
            F.lit(None).cast("long").alias("occ_corpus"), F.lit(None).cast("array<string>").alias("formes_corpus"))
        frames.append(shape(b, env, "B_corpus", label))

    # Section C : codes tapés dans le chat (occ = questions) et leur graphie dans le corpus (occ_corpus)
    for label in ["code_chiffres", "reference_segments", "sigle"]:
        qa = agg(tokens(qsrc, PATTERNS[label]), min_occ=1)
        ca = agg(tokens(corpus, PATTERNS[label]), min_occ=1).select(
            "cle", F.col("occ").alias("occ_corpus"), F.col("formes").alias("formes_corpus"))
        c = qa.join(ca, "cle", "left").select(
            "cle", F.lit(None).cast("string").alias("ref_catalogue"), "occ", "docs", "formes",
            "occ_corpus", "formes_corpus")
        frames.append(shape(c, env, "C_questions", label))

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

# Section A : REF du catalogue, citations exactes et sous une autre graphie
fig, axes = panels(len(ENVS_PRESENT))
for ax, env in zip(axes, ENVS_PRESENT):
    d = top_rows("A_reference", "reference", env, "occ_corpus").iloc[::-1]
    ax.barh(d.cle, d.occ, color=PALETTE[env], height=0.7, edgecolor="white", linewidth=0.5)
    ax.barh(d.cle, d.occ_corpus, left=d.occ, color=PALETTE[env], alpha=0.35, height=0.7,
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
