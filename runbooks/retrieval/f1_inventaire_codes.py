# Databricks notebook source
# Inventaire des acronymes et codes : tokens du corpus parsé, référentiel REF et questions du chat,
# écrits dans dev_proj.qualibot.f1_inventaire_codes (colonne env).
# Lecture seule sur <env>_proj.qualibot pour chaque env de ENVS ; seule la table cible est écrite.
#
# Principe : l'extraction est large et sans seuil de longueur. Tout bloc de lettres et chiffres relié par
# - _ / . est un token ; le classement en catégories et la forme du token (colonne structures) servent à trier
# ensuite, rien n'est écarté à l'extraction sauf les nombres seuls, les mots en minuscules et les mots capitalisés.
#
# Sections :
#   A_reference : une ligne par REF du catalogue.
#                 occ = citations exactes de la REF, occ_autre_graphie = même REF écrite autrement (espace, tiret,
#                 casse), occ_prefixe = REF suivie d'un suffixe collé par - _ / . (ex. MI-1413_FR).
#                 Une REF qui contient un espace est comptée en documents contenant la chaîne exacte (occ).
#   B_corpus    : une ligne par clé trouvée dans les documents. categorie = code_lettres_chiffres, sequence_espace,
#                 sigle, casse_mixte ou nombre_compose. occ = occurrences, docs = documents.
#   C_questions : une ligne par clé tapée dans le chat. occ = occurrences dans les questions, docs = questions,
#                 occ_corpus = occurrences de la même clé dans le corpus (NULL = absente du corpus),
#                 formes_corpus = leurs graphies.
#
# Colonnes communes :
#   cle         : token en majuscules, sans séparateur (ST-1027, st1027 et ST 1027 donnent ST1027).
#   formes      : graphies exactes vues dans la source de la ligne.
#   structures  : forme des graphies, A = majuscules, a = minuscules, 9 = chiffres, séparateurs conservés
#                 (ST-1027 donne A-9, Q0375MI donne A9A, eFNC donne aA).
#   nb_refs     : nombre de REF distinctes du corpus qui contiennent la clé.
#   refs_source : les 10 REF qui contiennent le plus la clé, avec le nombre de documents, "REF (n)".
#   urls_source : lien Intraqual de chacune de ces REF.

# COMMAND ----------
# Cellule 1 · paramètres et motifs

import time
from functools import reduce

from pyspark.sql import DataFrame
from pyspark.sql import Window
from pyspark.sql import functions as F

ENVS = ["dev", "uat"]   # environnements à inventorier : ["dev", "uat"], ["dev"] ou ["uat"]
TARGET = "dev_proj.qualibot.f1_inventaire_codes"
MIN_OCC = 1   # occurrences minimum dans le corpus pour une clé de la section B
MAX_SPACE_REFS = 2000   # au-delà, les REF contenant un espace ne sont pas comptées
INTRAQUAL_BASE = "https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref="

# Token : bloc de lettres et chiffres, relié au suivant par un seul - _ / . (ST-1027, MI-1413_FR, INAQ/560, 4.2.1).
# Aucune longueur minimale ou maximale. Le séparateur final (point de fin de phrase) n'est pas inclus.
TOKEN = r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*(?![A-Za-z0-9])"
# Séquence avec espaces : un token commençant par une lettre suivi d'un ou plusieurs nombres séparés par un espace
# (IAQ 20 00 04, BAC 5423, ISO 9001). Les dates et les mots suivis d'un nombre (mars 2026) en font partie : ils sont
# triés ensuite avec la colonne structures.
SEQUENCE = (r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_/.][A-Za-z0-9]+)*"
            r"(?: [0-9]+(?:[-_/.][0-9]+)*)+(?![A-Za-z0-9])")

CATEGORIES = ["code_lettres_chiffres", "sequence_espace", "sigle", "casse_mixte", "nombre_compose"]

TYPES = {
    "cle": "string", "ref_catalogue": "string", "occ": "long", "docs": "long",
    "formes": "array<string>", "structures": "array<string>",
    "occ_corpus": "long", "formes_corpus": "array<string>",
    "occ_autre_graphie": "long", "formes_autre_graphie": "array<string>",
    "occ_prefixe": "long", "formes_prefixe": "array<string>",
    "nb_refs": "long", "refs_source": "array<string>", "urls_source": "array<string>",
}

# COMMAND ----------
# Cellule 2 · fonctions (aucune écriture)


def with_cle(tok_df):
    """Ajoute la clé normalisée : majuscules, sans tiret, espace, point, slash ni underscore.

    :param tok_df: DataFrame avec une colonne tok
    :return: même DataFrame avec la colonne cle
    """
    return tok_df.withColumn("cle", F.upper(F.regexp_replace("tok", "[^A-Za-z0-9]", "")))


def structure(col):
    """Forme d'un token : A = majuscules, a = minuscules, 9 = chiffres, séparateurs conservés.

    :param col: colonne contenant le token
    :return: colonne de la forme
    """
    return F.regexp_replace(F.regexp_replace(F.regexp_replace(col, "[A-Z]+", "A"), "[a-z]+", "a"), "[0-9]+", "9")


def tokens(src, pattern):
    """Une ligne par occurrence du motif dans la colonne txt.

    :param src: DataFrame aux colonnes doc, doc_ref et txt
    :param pattern: expression régulière (syntaxe Java)
    :return: DataFrame aux colonnes doc, doc_ref et tok
    """
    pat = pattern.replace("\\", "\\\\")
    return src.select("doc", "doc_ref",
                      F.explode(F.expr(f"regexp_extract_all(txt, '{pat}', 0)")).alias("tok"))


def classified(src):
    """Tokens et séquences d'une source, avec leur catégorie et leur structure.

    Catégories : code_lettres_chiffres (lettres et chiffres), nombre_compose (chiffres et séparateurs seulement),
    sigle (au moins 2 lettres, toutes en majuscules), casse_mixte (au moins une minuscule et 2 majuscules),
    sequence_espace (token et nombres séparés par des espaces). Les nombres seuls, les mots en minuscules,
    les mots capitalisés et les majuscules isolées n'ont pas de catégorie et sont écartés.

    :param src: DataFrame aux colonnes doc, doc_ref et txt
    :return: DataFrame aux colonnes doc, doc_ref, tok, categorie et forme
    """
    t = F.col("tok")
    letters = F.length(F.regexp_replace(t, "[^A-Za-z]", ""))
    has_digit = t.rlike("[0-9]")
    has_lower = t.rlike("[a-z]")
    n_upper = F.length(F.regexp_replace(t, "[^A-Z]", ""))
    simple = (tokens(src, TOKEN)
              .withColumn("categorie",
                          F.when((letters > 0) & has_digit, "code_lettres_chiffres")
                          .when((letters == 0) & t.rlike("[-_/.]"), "nombre_compose")
                          .when((letters >= 2) & ~has_digit & ~has_lower, "sigle")
                          .when((letters > 0) & ~has_digit & has_lower & (n_upper >= 2), "casse_mixte"))
              .where(F.col("categorie").isNotNull()))
    seq = tokens(src, SEQUENCE).withColumn("categorie", F.lit("sequence_espace"))
    return simple.unionByName(seq).withColumn("forme", structure(F.col("tok")))


def keyed(tok_df):
    """Statistiques par catégorie et clé, avec les REF qui contiennent la clé et leurs liens Intraqual.

    :param tok_df: DataFrame aux colonnes doc, doc_ref, tok, categorie et forme
    :return: DataFrame aux colonnes categorie, cle, occ, docs, formes, structures, nb_refs, refs_source, urls_source
    """
    t = with_cle(tok_df)
    stats = (t.groupBy("categorie", "cle")
             .agg(F.count("*").alias("occ"), F.countDistinct("doc").alias("docs"),
                  F.array_sort(F.collect_set("tok")).alias("formes"),
                  F.array_sort(F.collect_set("forme")).alias("structures")))
    per_ref = (t.where(F.col("doc_ref").isNotNull())
               .groupBy("categorie", "cle", "doc_ref").agg(F.countDistinct("doc").alias("n"))
               .groupBy("categorie", "cle")
               .agg(F.sort_array(F.collect_list(F.struct("n", "doc_ref")), asc=False).alias("s"),
                    F.count("doc_ref").alias("nb_refs")))
    refs = per_ref.select(
        "categorie", "cle", "nb_refs",
        F.transform(F.slice("s", 1, 10),
                    lambda x: F.concat(x["doc_ref"], F.lit(" ("), x["n"].cast("string"), F.lit(")")))
        .alias("refs_source"),
        F.transform(F.slice("s", 1, 10),
                    lambda x: F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace(x["doc_ref"], " ", "%20")))
        .alias("urls_source"))
    return stats.join(refs, ["categorie", "cle"], "left")


def conform(df, env, section, categorie=None):
    """Ramène un DataFrame aux colonnes de la table ; une colonne absente de la section est nulle.

    :param df: DataFrame de la section
    :param env: environnement source (dev ou uat)
    :param section: nom de la section (A_reference, B_corpus, C_questions)
    :param categorie: catégorie fixe de la section, ou None pour utiliser la colonne categorie de df
    :return: DataFrame aux colonnes de la table
    """
    cols = [F.col(c).cast(t).alias(c) if c in df.columns else F.lit(None).cast(t).alias(c)
            for c, t in TYPES.items()]
    cat_col = F.lit(categorie) if categorie is not None else F.col("categorie")
    return df.select(F.lit(env).alias("env"), F.lit(section).alias("section"), cat_col.alias("categorie"),
                     *cols, F.current_timestamp().alias("date_run"))


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


def space_ref_counts(corpus, cat):
    """Documents contenant la chaîne exacte de chaque REF du catalogue qui contient un espace.

    :param corpus: DataFrame aux colonnes doc, doc_ref et txt
    :param cat: DataFrame de la colonne ref du catalogue
    :return: DataFrame aux colonnes ref et occ_espace
    """
    refs = [r["ref"] for r in cat.where(F.col("ref").contains(" ")).collect()]
    if len(refs) > MAX_SPACE_REFS:
        print(f"{len(refs)} REF avec espace : non comptées (MAX_SPACE_REFS = {MAX_SPACE_REFS})")
        refs = []
    counts = {}
    print(f"REF contenant un espace : {len(refs)}")
    for k in range(0, len(refs), 50):
        t0 = time.time()
        batch = refs[k:k + 50]
        row = corpus.agg(*[F.sum(F.when(F.instr(F.col("txt"), r) > 0, 1).otherwise(0)).alias(f"c{j}")
                           for j, r in enumerate(batch)]).collect()[0]
        counts.update({r: int(row[f"c{j}"] or 0) for j, r in enumerate(batch)})
        print(f"  REF avec espace {k + len(batch)}/{len(refs)} ({time.time() - t0:.0f} s)")
    return spark.createDataFrame(list(counts.items()), "ref string, occ_espace long")


def section_a(env, corpus):
    """Une ligne par REF du catalogue : citations exactes, autre graphie, suffixe collé.

    :param env: environnement (dev ou uat)
    :param corpus: DataFrame aux colonnes doc, doc_ref et txt
    :return: DataFrame de la section A, aux colonnes de la table
    """
    cat = spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL")
    refs_k = cat.withColumn("cle", F.upper(F.regexp_replace("ref", "[^A-Za-z0-9]", "")))
    # Chaque token portant un chiffre est comparé aux REF en entier et à chacun de ses débuts coupés sur un séparateur
    parts = F.split("tok", "(?=[-_/.])")
    exp = (tokens(corpus, TOKEN).where(F.col("tok").rlike("[0-9]"))
           .withColumn("parts", parts)
           .select("tok", F.size("parts").alias("n"),
                   F.posexplode(F.transform(F.sequence(F.lit(1), F.size("parts")),
                                            lambda i: F.array_join(F.slice("parts", 1, i), ""))).alias("i", "debut")))
    whole = (F.col("i") + 1) == F.col("n")
    same = F.col("tok") == F.col("ref")
    hits = (exp.withColumn("cle", F.upper(F.regexp_replace("debut", "[^A-Za-z0-9]", "")))
            .join(F.broadcast(refs_k), "cle")
            .groupBy("ref")
            .agg(F.sum(F.when(whole & same, 1).otherwise(0)).alias("exact"),
                 F.sum(F.when(whole & ~same, 1).otherwise(0)).alias("occ_autre_graphie"),
                 F.array_sort(F.collect_set(F.when(whole & ~same, F.col("tok")))).alias("formes_autre_graphie"),
                 F.sum(F.when(~whole, 1).otherwise(0)).alias("occ_prefixe"),
                 F.array_sort(F.collect_set(F.when(~whole, F.col("tok")))).alias("formes_prefixe")))
    empty = F.array().cast("array<string>")
    a = (cat.join(hits, "ref", "left").join(space_ref_counts(corpus, cat), "ref", "left")
         .select(F.col("ref").alias("cle"), F.col("ref").alias("ref_catalogue"),
                 F.coalesce("exact", "occ_espace", F.lit(0)).alias("occ"),
                 F.coalesce("occ_autre_graphie", F.lit(0)).alias("occ_autre_graphie"),
                 F.coalesce("formes_autre_graphie", empty).alias("formes_autre_graphie"),
                 F.coalesce("occ_prefixe", F.lit(0)).alias("occ_prefixe"),
                 F.coalesce("formes_prefixe", empty).alias("formes_prefixe"),
                 F.lit(1).alias("nb_refs"),
                 F.array(F.col("ref")).alias("refs_source"),
                 F.array(F.concat(F.lit(INTRAQUAL_BASE), F.regexp_replace("ref", " ", "%20"))).alias("urls_source")))
    return conform(a, env, "A_reference", "reference")


def build(env):
    """Construit les lignes des trois sections pour un environnement.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes de la table
    """
    corpus = src_corpus(env).cache()
    qsrc = src_questions(env).cache()
    print(f"[{env}] documents parsés : {corpus.count()} · questions du chat : {qsrc.count()}")

    t0 = time.time()
    corpus_stats = keyed(classified(corpus)).cache()
    print(f"[{env}] B : {corpus_stats.count()} clés ({time.time() - t0:.0f} s)")
    b = corpus_stats.where(F.col("occ") >= MIN_OCC)

    # Les questions sont rapprochées du corpus par la clé seule : la même clé peut y avoir une autre catégorie
    # (ST 1027 dans une question, ST-1027 dans un document).
    by_cle = (corpus_stats.groupBy("cle")
              .agg(F.sum("occ").alias("occ_corpus"),
                   F.array_sort(F.array_distinct(F.flatten(F.collect_list("formes")))).alias("formes_corpus"),
                   F.max_by(F.struct("nb_refs", "refs_source", "urls_source"), "occ").alias("r"))
              .select("cle", "occ_corpus", "formes_corpus", "r.nb_refs", "r.refs_source", "r.urls_source"))
    q = keyed(classified(qsrc)).select("categorie", "cle", "occ", "docs", "formes", "structures")
    c = q.join(by_cle, "cle", "left")

    t0 = time.time()
    a = section_a(env, corpus).cache()
    print(f"[{env}] A : {a.count()} REF ({time.time() - t0:.0f} s)")

    return reduce(DataFrame.unionByName, [
        a,
        conform(b, env, "B_corpus"),
        conform(c, env, "C_questions"),
    ])

# COMMAND ----------
# Cellule 3 · écriture : remplace les lignes des environnements de ENVS dans la table

parts = [build(env) for env in ENVS]
out = reduce(DataFrame.unionByName, parts)
# Une table écrite par une version précédente de ce notebook (sans la colonne structures) est recréée.
if spark.catalog.tableExists(TARGET) and "structures" not in spark.table(TARGET).columns:
    spark.sql(f"DROP TABLE {TARGET}")
if spark.catalog.tableExists(TARGET):
    for env in ENVS:
        spark.sql(f"DELETE FROM {TARGET} WHERE env = '{env}'")
    out.write.mode("append").option("mergeSchema", "true").saveAsTable(TARGET)
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

# Section A : REF du catalogue, citations exactes, autre graphie et suffixe collé
fig, axes = panels(len(ENVS_PRESENT))
for ax, env in zip(axes, ENVS_PRESENT):
    rows = inv[(inv.section == "A_reference") & (inv.env == env)].copy()
    rows["hors_exact"] = rows.occ_autre_graphie.fillna(0) + rows.occ_prefixe.fillna(0)
    d = rows.sort_values("hors_exact", ascending=False).head(TOP).iloc[::-1]
    ax.barh(d.cle, d.occ, color=PALETTE[env], height=0.7, edgecolor="white", linewidth=0.5)
    ax.barh(d.cle, d.occ_autre_graphie, left=d.occ, color=PALETTE[env], alpha=0.5, height=0.7,
            edgecolor="white", linewidth=0.5)
    ax.barh(d.cle, d.occ_prefixe, left=d.occ + d.occ_autre_graphie, color=PALETTE[env], alpha=0.25,
            height=0.7, edgecolor="white", linewidth=0.5)
    ax.legend(handles=[Patch(color=PALETTE[env], label="citation exacte"),
                       Patch(color=PALETTE[env], alpha=0.5, label="autre graphie"),
                       Patch(color=PALETTE[env], alpha=0.25, label="suffixe collé (_FR…)")],
              frameon=False, fontsize=9, loc="lower right")
    style(ax, f"{env.upper()} · REF citées autrement que sous leur forme exacte", "nombre de citations")
fig.tight_layout()
plt.show()

# Section B : candidats du corpus, par catégorie
for categorie in ["code_lettres_chiffres", "sequence_espace", "sigle", "casse_mixte", "nombre_compose"]:
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
