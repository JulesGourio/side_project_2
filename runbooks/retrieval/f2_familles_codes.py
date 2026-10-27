# Databricks notebook source
# Familles de codes : chaque clé de l'inventaire dev_proj.qualibot.f1_inventaire_codes (sections B_corpus et
# C_questions) reçoit une famille, dans l'ordre des règles ci-dessous ; la première qui s'applique gagne.
# Écrit dans dev_proj.qualibot.f2_familles_codes. Lecture seule ailleurs (doc_catalog de chaque env de ENVS).
#
# But : qu'il ne reste dans la famille "residu" que du bruit pur. Le résidu est trié par documents et regroupé par
# préfixe (cellule 5) ; chaque préfixe relu va dans NORME_PREFIXES (code voulu) ou BRUIT_PREFIXES, puis on relance.
#
# Familles, dans l'ordre d'application (action = ce que fera la normalisation) :
#   ref_catalogue        clé = REF du catalogue, avec ou sans suffixe de langue (QIS-46A-352 A, LdB IT-243,
#                        IAQ 04 04 05, PROLAT_R40_FR, PR/LAT/506 = PRLAT506)                          -> annoter
#   ref_sans_premier_mot clé = REF à espace sans son premier mot (IT-243 pour LdB IT-243)            -> annoter
#   ref_tronquee         clé = début d'une REF coupé sur un séparateur, avec un chiffre
#                        (QIS-46A-352 pour QIS-46A-352 A, DLV-31 pour DLV-31-5173)                    -> annoter
#   ref_variante         une graphie commence par une REF suivie d'un séparateur
#                        (MI-14019-1, QP-2086-Software, IF20037_DLV-31-5207-1Process)                 -> annoter
#   sigle                lettres seulement (DOA, E.A.S.A)                                               -> glossaire
#   nombre               chiffres seulement (4.2.1, dates)                                               -> rien
#   ref_hors_catalogue   même signature qu'une REF du catalogue, suffixe de langue retiré : préfixe en toutes
#                        lettres, puis 9 par chiffre et A par lettre (NS-1717 = NS9999 comme NS-1754 ;
#                        Q0697MI_FR = Q9999AA comme Q0375MI)                                          -> annoter
#   bruit_colonne        préfixe de BRUIT_PREFIXES suivi de chiffres seulement (COL12, OA21)             -> rien
#   bruit_sommaire       numéro collé à un mot de 3 lettres ou plus (2SCOPE4, 11REFERENCE, 821PROCESS)  -> rien
#   bruit_unite          nombre suivi d'une unité ou d'un ordinal de UNITES (5MM, 60KHZ, 1ST, 2ND)       -> rien
#   bruit_dimension      nombres reliés par X (125X80, 10X20X5)                                         -> rien
#   bruit_mot_nombre     séquence mot + nombre jamais écrite collée (Level 2, Figure 1, page 12)        -> rien
#   code_norme_piece     préfixe de NORME_PREFIXES (EN9100, NAS1149C0332R, A320)                        -> annoter
#   colle_doc_unique     écrit collé, sans séparateur, dans un seul document et jamais dans le chat :
#                        déjà un seul mot pour la recherche par mot clé (lots, numéros de série)        -> rien
#   residu               tout le reste : à relire                                                         -> a_classer
#
# Colonnes : env, cle, famille, action, ref (REF du catalogue qui a donné la famille), prefixe (lettres de tête),
# signature, a_separateur (une graphie contient - _ / . ou espace), docs (documents du corpus), q_docs (questions),
# occ, categories, formes (graphies du corpus et du chat), refs_source, urls_source, date_run.

# COMMAND ----------
# Cellule 1 · paramètres

from pyspark.sql import Window
from pyspark.sql import functions as F

ENVS = ["dev", "uat"]   # ["dev", "uat"], ["dev"] ou ["uat"]
SOURCE = "dev_proj.qualibot.f1_inventaire_codes"
TARGET = "dev_proj.qualibot.f2_familles_codes"

# Suffixes de langue retirés d'une REF pour sa clé (mêmes que doc_catalog.canon_ref de l'app).
LANGUES = ["FR", "EN", "GB", "MX", "BG", "CZ", "BR", "ES"]

# Préfixes de codes voulus hors catalogue : normes, pièces, avions, vus dans les exemples de l'inventaire.
NORME_PREFIXES = ["EN", "PREN", "ISO", "AS", "NAS", "MS", "ABS", "ASNA", "BACB", "NSA", "AITM", "AIPI", "AIPS",
                  "IPDA", "PART", "A", "DO", "ARP", "AMS", "ASTM", "MIL", "DIN", "SAE", "BMS", "IPC", "IEC", "NF",
                  "LN", "HST", "HL", "F7X", "CCR"]
# Préfixes de bruit : en-têtes de colonnes de tableaux, quand ils sont suivis de chiffres seulement.
BRUIT_PREFIXES = ["COL", "OA", "OC"]
# Unités et ordinaux qui suivent un nombre.
UNITES = ["MM", "CM", "DM", "M", "KM", "UM", "ML", "CL", "L", "MG", "G", "KG", "T", "N", "NM", "DAN", "KN", "BAR",
          "MBAR", "PSI", "PA", "KPA", "MPA", "HZ", "KHZ", "MHZ", "GHZ", "DB", "V", "KV", "MV", "A", "MA", "W", "KW",
          "H", "MIN", "MN", "S", "MS", "C", "K", "F", "IN", "FT", "LB", "LBS", "OZ", "PC", "PCS", "PT", "D", "X",
          "ST", "ND", "RD", "TH", "ER", "ERE", "EME", "E", "IEME", "O", "A"]

SEP = "[-_/. ]"

# COMMAND ----------
# Cellule 2 · fonctions (aucune écriture)


def canon(col):
    """Clé d'une chaîne : majuscules, sans aucun caractère autre que lettres et chiffres.

    :param col: colonne texte
    :return: colonne de la clé
    """
    return F.upper(F.regexp_replace(col, "[^A-Za-z0-9]", ""))


def sans_langue(col):
    """Clé d'une REF sans son suffixe de langue (MI-1413_FR donne MI1413).

    :param col: colonne de la REF
    :return: colonne de la clé
    """
    return canon(F.regexp_replace(F.trim(col), f"(?i)[-_. ]+({'|'.join(LANGUES)})$", ""))


def debuts(col):
    """Débuts d'une chaîne coupée devant chaque séparateur, sans la chaîne entière (A-1-2 donne A et A-1).

    :param col: colonne texte
    :return: colonne tableau des débuts, vide quand la chaîne n'a pas de séparateur
    """
    parts = F.split(col, f"(?={SEP})")
    return F.when(F.size(parts) > 1,
                  F.transform(F.sequence(F.lit(1), F.size(parts) - 1),
                              lambda i: F.array_join(F.slice(parts, 1, i), ""))
                  ).otherwise(F.array().cast("array<string>"))


def signature(name):
    """Signature d'une clé : lettres de tête en clair, puis 9 par chiffre et A par lettre (Q0375MI donne Q9999AA).

    :param name: nom de la colonne de la clé
    :return: colonne de la signature
    """
    tete = f"regexp_extract({name}, '^([A-Z]*)', 1)"
    reste = f"substring({name}, length({tete}) + 1)"
    return F.expr(f"concat({tete}, regexp_replace(regexp_replace({reste}, '[0-9]', '9'), '[A-Z]', 'A'))")


def catalogue(env):
    """Clés tirées des REF du catalogue, avec la famille qu'elles donnent et leur priorité.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes cle, famille, ref et prio (1 = la plus forte)
    """
    cat = spark.table(f"{env}_proj.qualibot.doc_catalog").select("ref").where("ref IS NOT NULL").distinct()
    entiere = cat.select(canon("ref").alias("cle"), "ref")
    sans_lg = cat.select(sans_langue("ref").alias("cle"), "ref")
    premier_mot = (cat.where(F.col("ref").contains(" "))
                   .select(sans_langue(F.regexp_replace("ref", r"^\S+\s+", "")).alias("cle"), "ref"))
    tronquee = (cat.select("ref", F.explode(debuts(F.trim("ref"))).alias("d"))
                .select(canon("d").alias("cle"), "ref").where(F.col("cle").rlike("[0-9]")))
    rows = (entiere.unionByName(sans_lg).withColumn("famille", F.lit("ref_catalogue")).withColumn("prio", F.lit(1))
            .unionByName(premier_mot.withColumn("famille", F.lit("ref_sans_premier_mot")).withColumn("prio", F.lit(2)))
            .unionByName(tronquee.withColumn("famille", F.lit("ref_tronquee")).withColumn("prio", F.lit(3))))
    w = Window.partitionBy("cle").orderBy("prio", "ref")
    return rows.where(F.length("cle") > 0).withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


def cles(env):
    """Une ligne par clé de l'environnement, sections B_corpus et C_questions réunies.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes cle, docs, q_docs, occ, categories, formes, refs_source et urls_source
    """
    inv = spark.table(SOURCE).where((F.col("env") == env) & F.col("section").isin("B_corpus", "C_questions"))
    b = F.col("section") == "B_corpus"
    return (inv.groupBy("cle")
            .agg(F.max(F.when(b, F.col("docs"))).alias("docs"),
                 F.sum(F.when(~b, F.col("docs"))).alias("q_docs"),
                 F.sum(F.when(b, F.col("occ"))).alias("occ"),
                 F.array_sort(F.collect_set("categorie")).alias("categories"),
                 F.array_sort(F.array_distinct(F.flatten(F.collect_list("formes")))).alias("formes"),
                 F.max_by(F.struct("refs_source", "urls_source"), F.when(b, F.col("docs"))).alias("r"))
            .select("cle", F.coalesce("docs", F.lit(0)).alias("docs"), F.coalesce("q_docs", F.lit(0)).alias("q_docs"),
                    F.coalesce("occ", F.lit(0)).alias("occ"), "categories", "formes",
                    "r.refs_source", "r.urls_source"))


def familles(env):
    """Famille et action de chaque clé de l'environnement.

    :param env: environnement (dev ou uat)
    :return: DataFrame aux colonnes de la table cible
    """
    k = cles(env)
    cat = catalogue(env)
    refs_entieres = cat.where("famille = 'ref_catalogue'").select(F.col("cle").alias("d"), F.col("ref").alias("ref_v"))
    variante = (k.select("cle", F.explode("formes").alias("f"))
                .select("cle", F.explode(debuts("f")).alias("d"))
                .select("cle", canon("d").alias("d"))
                .join(F.broadcast(refs_entieres), "d")
                .groupBy("cle").agg(F.min("ref_v").alias("ref_v")))
    sig_cat = (cat.where("famille = 'ref_catalogue'")
               .select(signature("cle").alias("signature"), "ref")
               .where(F.col("signature").rlike("^[A-Z]+.*9"))
               .groupBy("signature").agg(F.min("ref").alias("ref_s")))
    # Chaque graphie est comparée sans son suffixe de langue (Q0697MI_FR a la signature de Q0697MI).
    hors_cat = (k.select("cle", F.explode("formes").alias("f"))
                .select("cle", sans_langue("f").alias("sl"))
                .select("cle", signature("sl").alias("signature"))
                .join(F.broadcast(sig_cat), "signature")
                .groupBy("cle").agg(F.min("ref_s").alias("ref_s")))

    c = F.col("cle")
    prefixe = F.regexp_extract(c, "^([A-Z]*)", 1)
    a_sep = F.exists("formes", lambda f: f.rlike(SEP))
    lettres = c.rlike("[A-Z]")
    chiffres = c.rlike("[0-9]")
    only_seq = F.array_join("categories", ",") == "sequence_espace"
    seq_jamais_collee = only_seq & F.forall("formes", lambda f: f.contains(" "))
    mot_tete = F.forall("formes", lambda f: f.rlike(r"^[A-Za-z]+ "))

    df = (k.join(cat.select("cle", F.col("famille").alias("fam_cat"), F.col("ref").alias("ref_c")), "cle", "left")
          .join(variante, "cle", "left")
          .withColumn("prefixe", prefixe)
          .withColumn("signature", signature("cle"))
          .join(hors_cat, "cle", "left")
          .withColumn("a_separateur", a_sep))

    famille = (F.when(F.col("fam_cat").isNotNull(), F.col("fam_cat"))
               .when(F.col("ref_v").isNotNull(), "ref_variante")
               .when(~chiffres, "sigle")
               .when(~lettres, "nombre")
               .when(F.col("ref_s").isNotNull() & (F.length("prefixe") > 0), "ref_hors_catalogue")
               .when(c.rlike(f"^({'|'.join(BRUIT_PREFIXES)})[0-9]+$"), "bruit_colonne")
               .when(c.rlike("^[0-9]+[A-Z]{3,}"), "bruit_sommaire")
               .when(c.rlike(f"^[0-9]+({'|'.join(sorted(set(UNITES), key=len, reverse=True))})$"), "bruit_unite")
               .when(c.rlike("^[0-9]+(X[0-9]+)+$"), "bruit_dimension")
               .when(seq_jamais_collee & mot_tete & ~F.col("prefixe").isin(NORME_PREFIXES), "bruit_mot_nombre")
               .when(F.col("prefixe").isin(NORME_PREFIXES), "code_norme_piece")
               .when(~F.col("a_separateur") & (F.col("docs") <= 1) & (F.col("q_docs") == 0), "colle_doc_unique")
               .otherwise("residu"))
    action = (F.when(F.col("famille").startswith("ref_") | (F.col("famille") == "code_norme_piece"), "annoter")
              .when(F.col("famille") == "sigle", "glossaire")
              .when(F.col("famille") == "residu", "a_classer")
              .otherwise("rien"))
    return (df.withColumn("famille", famille).withColumn("action", action)
            .select(F.lit(env).alias("env"), "cle", "famille", "action",
                    F.coalesce("ref_c", "ref_v", "ref_s").alias("ref"), "prefixe", "signature", "a_separateur",
                    "docs", "q_docs", "occ", "categories", "formes", "refs_source", "urls_source",
                    F.current_timestamp().alias("date_run")))

# COMMAND ----------
# Cellule 3 · écriture : remplace les lignes des environnements de ENVS

out = None
for env in ENVS:
    part = familles(env)
    out = part if out is None else out.unionByName(part)
if spark.catalog.tableExists(TARGET):
    for env in ENVS:
        spark.sql(f"DELETE FROM {TARGET} WHERE env = '{env}'")
    out.write.mode("append").option("mergeSchema", "true").saveAsTable(TARGET)
else:
    out.write.mode("overwrite").saveAsTable(TARGET)
print(f"écrit dans {TARGET} : {ENVS}")

# COMMAND ----------
# Cellule 4 · couverture : clés, documents cumulés et part du total par famille

display(spark.sql(f"""
SELECT env, famille, action,
       COUNT(*) AS cles,
       SUM(docs) AS docs_cumules,
       SUM(occ) AS occurrences,
       SUM(CASE WHEN q_docs > 0 THEN 1 ELSE 0 END) AS cles_du_chat,
       ROUND(100 * SUM(docs) / SUM(SUM(docs)) OVER (PARTITION BY env), 1) AS pct_docs,
       SLICE(SORT_ARRAY(COLLECT_LIST(STRUCT(docs, cle)), false), 1, 8) AS exemples
FROM {TARGET}
GROUP BY env, famille, action
ORDER BY env, action, docs_cumules DESC
"""))

# COMMAND ----------
# Cellule 5 · résidu : par préfixe (à classer dans NORME_PREFIXES ou BRUIT_PREFIXES), puis clé par clé

display(spark.sql(f"""
SELECT env, CASE WHEN prefixe = '' THEN '(commence par un chiffre)' ELSE prefixe END AS prefixe,
       COUNT(*) AS cles, SUM(docs) AS docs_cumules, SUM(q_docs) AS questions,
       SLICE(SORT_ARRAY(COLLECT_LIST(STRUCT(docs, formes[0] AS forme)), false), 1, 10) AS exemples
FROM {TARGET}
WHERE famille = 'residu'
GROUP BY env, prefixe
ORDER BY docs_cumules DESC
LIMIT 300
"""))

display(spark.sql(f"""
SELECT env, cle, docs, q_docs, signature, categories, SLICE(formes, 1, 6) AS formes,
       SLICE(refs_source, 1, 3) AS refs_source, SLICE(urls_source, 1, 3) AS urls_source
FROM {TARGET}
WHERE famille = 'residu'
ORDER BY docs DESC, q_docs DESC
LIMIT 300
"""))

# COMMAND ----------
# Cellule 6 · contrôle : clés tapées dans le chat, avec leur famille (une clé du chat en bruit est une erreur)

display(spark.sql(f"""
SELECT env, famille, cle, q_docs, docs, ref, SLICE(formes, 1, 6) AS formes
FROM {TARGET}
WHERE q_docs > 0 AND famille NOT IN ('sigle')
ORDER BY env, famille, q_docs DESC
"""))
