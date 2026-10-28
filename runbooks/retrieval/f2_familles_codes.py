# Databricks notebook source
# MAGIC %md
# MAGIC # Familles de codes
# MAGIC
# MAGIC Chaque clé de l'inventaire `dev_proj.qualibot.f1_inventaire_codes` (sections `B_corpus` et `C_questions`) reçoit **une famille** : la première règle du tableau qui s'applique gagne. Résultat dans `dev_proj.qualibot.f2_familles_codes`. Lecture seule ailleurs (`doc_catalog` de chaque env de `ENVS`).
# MAGIC
# MAGIC **Règle de sécurité.** `annoter` ajoute la clé collée à côté du texte (`SF-1027 [SF1027]`), sans rien remplacer. `rien` laisse le texte indexé tel quel. Toute clé qu'aucune règle de bruit ne reconnaît est annotée (`residu`) : un code n'est jamais oublié, au pire une clé inutile est ajoutée.
# MAGIC
# MAGIC **Morceaux.** Une graphie est coupée devant chaque séparateur (`-` `_` `/` `.` espace) et entre un chiffre et la lettre qui le suit : `MI-13991Table` donne `MI`, `-13991`, `Table`. Une *suite* = plusieurs morceaux consécutifs.
# MAGIC
# MAGIC **Signature.** Lettres de tête en clair, puis `9` par chiffre et `A` par lettre : `Q0375MI` donne `Q9999AA`.
# MAGIC
# MAGIC | # | Famille | Règle | Exemples | Action |
# MAGIC |---|---|---|---|---|
# MAGIC | 1 | `ref_catalogue` | clé = REF du catalogue, avec ou sans suffixe de langue | `QIS-46A-352 A`, `IAQ 04 04 05`, `PROLAT_R40_FR`, `PR/LAT/506` | annoter |
# MAGIC | 2 | `ref_sans_premier_mot` | REF à espace sans son premier mot, si le reste = lettres + séparateur + chiffres | `IT-243` (LdB IT-243) | annoter |
# MAGIC | 3 | `ref_sans_indice` | REF sans sa dernière partie en lettres | `QIS-46A-352` (QIS-46A-352 A) | annoter |
# MAGIC | 4 | `ref_variante` | une suite de morceaux d'une graphie est une REF | `MI-14019-1`, `PRO_Q0014NF_EN`, `MI-13991Table 3` | annoter |
# MAGIC | 5 | `sigle` | lettres seulement | `DOA`, `E.A.S.A` | glossaire |
# MAGIC | 6 | `code_norme_piece` | clé de `NORME_CLES` | `21J` (Part 21 sous-partie J) | annoter |
# MAGIC | 7 | `nombre` | chiffres seulement | `4.2.1`, dates | rien |
# MAGIC | 8 | `ref_hors_catalogue` | une suite de morceaux, sans suffixe de langue, a la signature d'une REF | `NS-1717` (comme NS-1754), `SF-1027` (comme SF-1029) | annoter |
# MAGIC | 9 | `bruit_url_fichier` | adresse, chemin ou nom de fichier | `prtouintraqual1/intraqual/…`, `x.pdf` | rien |
# MAGIC | 10 | `bruit_colonne` | préfixe de `BRUIT_PREFIXES` + chiffres seulement | `COL12`, `OA21` | rien |
# MAGIC | 11 | `bruit_sommaire` | numéro collé à un mot de 3 lettres ou plus | `2SCOPE4`, `11REFERENCE` | rien |
# MAGIC | 12 | `bruit_unite` | nombre + unité ou ordinal de `UNITES` | `5MM`, `60KHZ`, `1ST`, `3D` | rien |
# MAGIC | 13 | `bruit_dimension` | nombres reliés par X | `125X80` | rien |
# MAGIC | 14 | `code_tete_chiffres` | nombre + séparateur + lettres et chiffres mêlés | `2024-T3`, `15-5PH` | annoter |
# MAGIC | 15 | `bruit_puce` | clé = numéro + une ou deux lettres | `3.P`, `1-b`, `10P` | rien |
# MAGIC | 16 | `bruit_date` | mois + nombre | `July 2024`, `Jan/31/2024` | rien |
# MAGIC | 17 | `code_norme_piece` | préfixe de `NORME_PREFIXES` | `EN 9100`, `PART 21`, `CS 25` | annoter |
# MAGIC | 18 | `code_court` | une ou deux lettres + un ou deux chiffres | `B1`, `R30`, `D1` | annoter |
# MAGIC | 19 | `bruit_mot_petit_nombre` | mot de 3 lettres ou plus + nombres d'un ou deux chiffres, sauf préfixe de REF | `LEVEL 2`, `TABLE 1/1`, `Figure 13` | rien |
# MAGIC | 20 | `code_segmente` | commence par des majuscules, un séparateur, 3 chiffres à la suite | `BAC 5578`, `NE 40-012`, `STM-0002396`, `A 320` | annoter |
# MAGIC | 21 | `bruit_mot_nombre` | mot + nombre, jamais écrit collé | `protection 999` | rien |
# MAGIC | 22 | `code_colle` | aucune graphie n'a de séparateur : déjà un seul mot pour le BM25 | `IS0024879M`, `F7X` | rien |
# MAGIC | 23 | `residu` | tout le reste, à relire pour y trouver du bruit | `TA6V`, `Partie-145.A.90` | annoter |
# MAGIC
# MAGIC **Colonnes de la table** : `env`, `cle`, `famille`, `action`, `ref` (REF qui a donné la famille), `cle_base` (la suite de morceaux reconnue : `MI13991` pour `MI-13991Table 3`, `Q0697MI` pour `Q0697MI_FR` ; sinon la clé), `prefixe` (lettres de tête), `signature`, `a_separateur` (une graphie contient `-` `_` `/` `.` ou espace), `docs` (documents du corpus), `q_docs` (questions du chat), `occ`, `categories`, `formes` (graphies du corpus et du chat), `refs_source`, `urls_source`, `date_run`.

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
                  "IPDA", "PART", "DO", "ARP", "AMS", "ASTM", "MIL", "DIN", "SAE", "BMS", "IPC", "IEC", "NF",
                  "LN", "HST", "HL", "CCR", "CS", "AMC"]
# Clés voulues dont la forme ressemble à du bruit (Part 21 sous-parties J et G).
NORME_CLES = ["21J", "21G"]
# Préfixes de bruit : en-têtes de colonnes de tableaux, quand ils sont suivis de chiffres seulement.
BRUIT_PREFIXES = ["COL", "OA", "OC"]
# Unités et ordinaux qui suivent un nombre.
UNITES = ["MM", "CM", "DM", "M", "KM", "UM", "ML", "CL", "L", "MG", "G", "KG", "T", "N", "NM", "DAN", "KN", "BAR",
          "MBAR", "PSI", "PA", "KPA", "MPA", "HZ", "KHZ", "MHZ", "GHZ", "DB", "V", "KV", "MV", "A", "MA", "W", "KW",
          "H", "MIN", "MN", "S", "MS", "C", "K", "F", "IN", "FT", "LB", "LBS", "OZ", "PC", "PCS", "PT", "D", "X",
          "ST", "ND", "RD", "TH", "ER", "ERE", "EME", "E", "IEME", "O", "A"]

SEP = "[-_/. ]"
# Coupe d'une graphie en morceaux : devant chaque séparateur et entre un chiffre et la lettre qui le suit.
COUPE = "(?=[-_/. ])|(?<=[0-9])(?=[A-Za-z])"
# Mois (français, anglais) suivi d'un nombre : une date.
MOIS = (r"(?i)^(jan|janv|fe[bv]|fevr|mar|mars|apr|avr|may|mai|jun|juin|jul|juil|aug|aou|sep|sept|oct|nov|dec)"
        r"[a-z]*[ ./-]{0,3}[0-9]")
# Adresse, chemin ou nom de fichier.
URL_FICHIER = (r"(?i)(^https?|^www\.|\.(aspx?|local|corp|com|net|org|html?|pdf|docx?|xlsx?|pptx?|png|jpe?g|txt|csv)"
               r"($|[/.?#])|(?-i:/[a-z]{2,}/))")

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


def suites(col):
    """Toutes les suites de morceaux consécutifs d'une graphie (PRO_Q0014NF_EN donne PRO, PRO_Q0014, Q0014NF, ...).

    :param col: colonne de la graphie
    :return: colonne tableau des suites, la graphie entière comprise
    """
    parts = F.split(col, COUPE)
    n = F.size(parts)
    return F.flatten(F.transform(
        F.sequence(F.lit(1), n),
        lambda i: F.transform(F.sequence(i, n), lambda j: F.array_join(F.slice(parts, i, j - i + 1), ""))))


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
    code = F.col("cle").rlike("[A-Z]") & F.col("cle").rlike("[0-9]")
    entiere = cat.select(canon("ref").alias("cle"), "ref")
    sans_lg = cat.select(sans_langue("ref").alias("cle"), "ref")
    reste = F.regexp_replace(F.trim("ref"), r"^\S+\s+", "")
    premier_mot = (cat.where(F.trim("ref").contains(" ") & reste.rlike("^[A-Za-z]+[-_/.][0-9]"))
                   .select(sans_langue(reste).alias("cle"), "ref").where(code))
    indice = (cat.select("ref", F.explode(debuts(F.trim("ref"))).alias("d"))
              .where(canon(F.expr("substring(trim(ref), length(d) + 1)")).rlike("^[A-Z]+$"))
              .select(canon("d").alias("cle"), "ref").where(code))
    rows = (entiere.unionByName(sans_lg).withColumn("famille", F.lit("ref_catalogue")).withColumn("prio", F.lit(1))
            .unionByName(premier_mot.withColumn("famille", F.lit("ref_sans_premier_mot")).withColumn("prio", F.lit(2)))
            .unionByName(indice.withColumn("famille", F.lit("ref_sans_indice")).withColumn("prio", F.lit(3))))
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
    refs_entieres = cat.where("famille = 'ref_catalogue'").select(F.col("cle").alias("s"), F.col("ref").alias("ref_v"))
    sig_cat = (cat.where("famille = 'ref_catalogue'")
               .select(signature("cle").alias("signature"), "ref")
               .where(F.col("signature").rlike("^[A-Z]+.*9"))
               .groupBy("signature").agg(F.min("ref").alias("ref_s")))
    morceaux = (k.select("cle", F.explode("formes").alias("f"))
                .select("cle", F.explode(suites("f")).alias("m"))
                .where(F.col("m").rlike("[A-Za-z]") & F.col("m").rlike("[0-9]"))
                .select("cle", canon("m").alias("s"), sans_langue("m").alias("sl"))
                .distinct().cache())
    variante = (morceaux.join(F.broadcast(refs_entieres), "s")
                .groupBy("cle").agg(F.max_by(F.struct("ref_v", "s"), F.length("s")).alias("v"))
                .select("cle", "v.ref_v", F.col("v.s").alias("base_v")))
    hors_cat = (morceaux.select("cle", "sl", signature("sl").alias("signature"))
                .join(F.broadcast(sig_cat), "signature")
                .groupBy("cle").agg(F.max_by(F.struct("ref_s", "sl"), F.length("sl")).alias("h"))
                .select("cle", "h.ref_s", F.col("h.sl").alias("base_s")))

    c = F.col("cle")
    lettres = c.rlike("[A-Z]")
    chiffres = c.rlike("[0-9]")
    toutes = lambda motif: F.forall("formes", lambda f: f.rlike(motif))
    une = lambda motif: F.exists("formes", lambda f: f.rlike(motif))
    unites = "|".join(sorted(set(UNITES), key=len, reverse=True))
    # Lettres de tête des REF du catalogue : un mot qui en fait partie n'est jamais du bruit (DLV-31).
    prefixes_ref = [r["p"] for r in cat.where("famille = 'ref_catalogue'")
                    .select(F.regexp_extract("cle", "^([A-Z]+)", 1).alias("p")).where("p <> ''").distinct().collect()]

    df = (k.join(cat.select("cle", F.col("famille").alias("fam_cat"), F.col("ref").alias("ref_c")), "cle", "left")
          .join(variante, "cle", "left")
          .join(hors_cat, "cle", "left")
          .withColumn("prefixe", F.regexp_extract(c, "^([A-Z]*)", 1))
          .withColumn("signature", signature("cle"))
          .withColumn("a_separateur", une(SEP)))

    famille = (F.when(F.col("fam_cat").isNotNull(), F.col("fam_cat"))
               .when(F.col("ref_v").isNotNull(), "ref_variante")
               .when(~chiffres, "sigle")
               .when(c.isin(NORME_CLES), "code_norme_piece")
               .when(~lettres, "nombre")
               .when(F.col("ref_s").isNotNull(), "ref_hors_catalogue")
               .when(une(URL_FICHIER), "bruit_url_fichier")
               .when(c.rlike(f"^({'|'.join(BRUIT_PREFIXES)})[0-9]+$"), "bruit_colonne")
               .when(c.rlike("^[0-9]+[A-Z]{3,}"), "bruit_sommaire")
               .when(c.rlike(f"^[0-9]+({unites})$"), "bruit_unite")
               .when(c.rlike("^[0-9]+(X[0-9]+)+$"), "bruit_dimension")
               .when(une("^[0-9]+[-_/. ]([A-Za-z]+[0-9]|[0-9]+[A-Za-z])"), "code_tete_chiffres")
               .when(c.rlike("^[0-9]{1,3}[A-Z]{1,2}$"), "bruit_puce")
               .when(une(MOIS), "bruit_date")
               .when(F.col("prefixe").isin(NORME_PREFIXES), "code_norme_piece")
               .when(c.rlike("^[A-Z]{1,2}[0-9]{1,2}$"), "code_court")
               .when(~F.col("prefixe").isin(prefixes_ref)
                     & (c.rlike("^[A-Z]{3,}[0-9]{1,2}$")
                        | toutes(r"^[A-Za-z]{3,}[-_/. ]{0,3}[0-9]{1,2}([-_/. ]{1,3}[0-9]{1,2})*$")),
                     "bruit_mot_petit_nombre")
               .when(une(r"^[A-Z][A-Z0-9]*([-_/. ]{1,3}[A-Za-z0-9]+)+$") & une("[0-9]{3}") & une(SEP),
                     "code_segmente")
               .when(toutes(r"^[A-Za-z]+ [0-9]"), "bruit_mot_nombre")
               .when(~F.col("a_separateur"), "code_colle")
               .otherwise("residu"))
    action = (F.when(F.col("famille").startswith("ref_") | F.col("famille").startswith("code_")
                     | (F.col("famille") == "residu"), "annoter")
              .when(F.col("famille") == "sigle", "glossaire")
              .otherwise("rien"))
    action = F.when(F.col("famille") == "code_colle", "rien").otherwise(action)
    return (df.withColumn("famille", famille).withColumn("action", action)
            .select(F.lit(env).alias("env"), "cle", "famille", "action",
                    F.coalesce("ref_c", "ref_v", "ref_s").alias("ref"),
                    F.when(F.col("famille") == "ref_variante", F.col("base_v"))
                    .when(F.col("famille") == "ref_hors_catalogue", F.col("base_s"))
                    .otherwise(c).alias("cle_base"),
                    "prefixe", "signature", "a_separateur",
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

# MAGIC %md
# MAGIC ### Couverture par famille
# MAGIC Une ligne par env et famille : nombre de clés, documents cumulés, part des documents (`pct_docs`), clés tapées dans le chat (`cles_du_chat`), 8 exemples triés par documents.

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

# MAGIC %md
# MAGIC ### Résidu
# MAGIC Premier tableau : le résidu regroupé par préfixe (lettres de tête), trié par documents. Second tableau : clé par clé. Un préfixe de bruit va dans `BRUIT_PREFIXES`, un code voulu dans `NORME_PREFIXES`, puis on relance.

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

# MAGIC %md
# MAGIC ### Clés du chat
# MAGIC Toutes les clés tapées dans le chat (hors sigles), avec leur famille. Une clé du chat classée en bruit doit être relue : un vrai code n'y a pas sa place.

# COMMAND ----------
# Cellule 6 · contrôle : clés tapées dans le chat, avec leur famille (une clé du chat en bruit est une erreur)

display(spark.sql(f"""
SELECT env, famille, cle, q_docs, docs, ref, SLICE(formes, 1, 6) AS formes
FROM {TARGET}
WHERE q_docs > 0 AND famille NOT IN ('sigle')
ORDER BY env, famille, q_docs DESC
"""))
