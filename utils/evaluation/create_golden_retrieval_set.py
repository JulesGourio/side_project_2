# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %md
# MAGIC # PROJ EVAL_GOLDEN_RETRIEVAL
# MAGIC
# MAGIC **Description:**
# MAGIC Builds the golden set used to measure the retrieval quality of the Qualibot chatbot: one row per question,
# MAGIC with the REF of the documents that the search must return (`expected_refs`).
# MAGIC The list `GOLDEN_SET` (section *Config Other*) is the source of truth, versioned with the code; the table is
# MAGIC fully rewritten at every run.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC * REF are stored in canonical form (upper case, no dash, space or language suffix), as `canon_ref` produces them
# MAGIC   for the retrieved documents in `retrieval_eval.py`.
# MAGIC * A question without expected document (`kind` = `no_document` or `to_complete`) is kept for the answer judges
# MAGIC   but ignored by Recall@K / MRR@K.
# MAGIC * An expected REF absent from the index makes the question fail whatever the engine does: it is flagged (AMBER).
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - First task of the job `Z_4_Qualibot_Retrieval_Eval`, then `retrieval_eval.py` reads the table. Also run by hand
# MAGIC   when the golden set changes.
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{pipeline_read_env}_proj.qualibot.chunks` (indexed REF, for the check)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{pipeline_write_env}_proj.qualibot.<golden_table_name>` (default `eval_golden_retrieval`)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Technical Debt
# MAGIC - The check of the REF reads `chunks`, not the Vector Search index itself: a document chunked but not yet synced
# MAGIC   to the index passes the check.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports
# MAGIC The repository root (two folders above this notebook) is added to `sys.path` for `leap_utils`.

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.getcwd())))

import pyspark.sql.functions as f
from pyspark.sql import Column

# COMMAND ----------

# MAGIC %md
# MAGIC ### Config LEAP Function Imports

# COMMAND ----------

from leap_utils.data_asset import table_utils
from leap_utils.common import logger

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets

# COMMAND ----------

# MAGIC %md
# MAGIC ### Pipeline Environments

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.text("pipeline_write_env", "dev")
PIPELINE_WRITE_ENV = dbutils.widgets.get("pipeline_write_env")

dbutils.widgets.text("pipeline_read_env", "dev")
PIPELINE_READ_ENV = dbutils.widgets.get("pipeline_read_env") or PIPELINE_WRITE_ENV

dbutils.widgets.text("reference_read_env", "prod")
REFERENCE_READ_ENV = dbutils.widgets.get("reference_read_env")

dbutils.widgets.dropdown("bypass_quality_checks", "false", ["false", "true"])
BYPASS_QUALITY_CHECKS = dbutils.widgets.get("bypass_quality_checks").lower() == "true"

dbutils.widgets.text("debug", "false")
DEBUG = dbutils.widgets.get("debug").lower() == "true"

dbutils.widgets.dropdown("log_level", defaultValue="info", choices=["debug", "info", "warning", "error", "critical"])
LOG_LEVEL = dbutils.widgets.get("log_level")

log = logger.setup_applevel_logger(log_level=logger.LOGGER_MAPPING[LOG_LEVEL.lower()])

# COMMAND ----------

# MAGIC %md
# MAGIC ### Tables

# COMMAND ----------

dbutils.widgets.text("golden_table_name", "eval_golden_retrieval")
OUTPUT_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.{dbutils.widgets.get('golden_table_name').strip()}"
CHUNKS_TABLE = f"{PIPELINE_READ_ENV}_proj.qualibot.chunks"

log.info(f"Output: {OUTPUT_TABLE} | REF check against: {CHUNKS_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Other
# MAGIC ### Golden set
# MAGIC `(question, expected REF, language, kind, division)`.
# MAGIC `kind`: `positive` (expected documents), `no_document` (the right behaviour is to say that no document covers
# MAGIC the topic), `to_complete` (expected documents still to fill in). `division`: `ALL`, `AS` or `IS`.

# COMMAND ----------

GOLDEN_SET = [
    ("Comment dois-je gérer les produits non conformes ?", ["Q0126QP", "PRLAT105"], "fr", "positive", "ALL"),
    ("Comment gérer la tracabilité chez Latecoere?", ["IAQ080002"], "fr", "positive", "ALL"),
    ("Quels sont les documents citant la SF-1027?", ["Q0102QP"], "fr", "positive", "ALL"),
    ("Que veut dire FAI?", ["Q0102QP"], "fr", "positive", "ALL"),
    ("How to do a NCR report ?", [], "en", "to_complete", "ALL"),
    ("A quoi correspond le code protection 999 de Dassault", [], "fr", "no_document", "ALL"),
    ("Dans le processus Control of Archiving, quelle est la duree de conservation prevue pour les documents "
     "d'interchangeabilite LOP 197 ?", ["Q0040QR"], "fr", "positive", "ALL"),
    ("Quel est le processus de qualification pour faire de la peinture?",
     ["IF20016", "Q0196QP", "Q0298QR", "Q0674MI"], "fr", "positive", "ALL"),
    ("Can we do a part without FAI if the FAI is less than 2 years in the previous site?",
     ["PRLAT538", "Q0102QP"], "en", "positive", "ALL"),
    ("Que signifie l'acronyme APO ?", ["INAPO0006"], "fr", "positive", "ALL"),
    ("Quelle est la regle concernant les FAI pour des pieces qui n'ont pas ete fabriquees depuis plus de 2 ans?",
     ["INAQ619", "PRLAT538", "Q0102QP"], "fr", "positive", "ALL"),
    ("Trouve moi le template du CMP", ["NF10065"], "fr", "positive", "ALL"),
    ("Cuales son los requisitos de agudeza visual para el personal de inspeccion visual?",
     ["MR1226", "QP1518"], "es", "positive", "ALL"),
    ("Donne moi le lien de l'OPEX Sharepoint", ["INAQ742"], "fr", "positive", "ALL"),
    ("C'est quoi le process de gestion des différentiels pour Airbus ?", ["INAQ756"], "fr", "positive", "ALL"),
    ("v SAPU v modulu MM mame nezauctovanou zasobu na 'return delivery' kterou jsme tam zauctovali pohybem 561. "
     "Jak ji mame oductovat ze zasob?", ["MI13841"], "cs", "positive", "ALL"),
    ("What is the approval process for deviations from engineering specifications?",
     ["MI1100", "Q0451MQ"], "en", "positive", "ALL"),
    # Promoted from synthetic set (2026-10-29)
    ("Dans le système de contrôle qualité, qui planifie les audits internes et à quelle fréquence "
     "chaque exigence doit-elle être vérifiée ?", ["QP2154"], "fr", "positive", "AS"),
    ("Dans le processus SAP de notification qualité « Non Conformity Report », comment le rôle d'Inspector "
     "détermine-t-il les actions autorisées et quelles transactions permettent de gérer les notifications ?",
     ["GO1508"], "fr", "positive", "ALL"),
    ("En urgence, quelle instruction consulter pour un problème de qualité de l'eau utilisée dans les "
     "processus chimiques et électrochimiques ?", ["MI14139", "DLV315162"], "fr", "positive", "ALL"),
    # Promoted from feedback (real user questions, 2026-10-29)
    ("Comment rejeter une CRE", ["GO1364"], "fr", "positive", "ALL"),
    ("Je suis acheteur, j'aimerais savoir comment faire pour sélectionner un nouveau fournisseur "
     "et pour l'intégrer à la liste des fournisseurs approuvés", ["QP2198"], "fr", "positive", "ALL"),
    ("Quelle est la procédure de sélection de fournisseur du processus achat ?", ["QP1457"], "fr", "positive", "ALL"),
    ("Qui identifie les requirements applicables à un fournisseur, qui les cascade au fournisseur ?",
     ["Q0062MI"], "fr", "positive", "ALL"),
    # Promoted from synthetic (2026-10-29)
    ("Moyen de production : le PV de recette vient d'être signé par le site receveur, que signifie ce jalon "
     "et qui est responsable maintenant ?", ["PRLAT508"], "fr", "positive", "IS"),
    ("Dans le processus de paramétrage article CA, après le double pass, quelles valeurs doivent apparaître "
     "pour les articles IS01, IS02 ou IS03 dans les colonnes 14 à 17 ?", ["FDAQL2178"], "fr", "positive", "ALL"),
    ("Pour le processus NADCAP, que faire si le pré-audit n'est pas planifié à temps avant l'audit externe ?",
     ["QP2154", "Q0375MI"], "fr", "positive", "AS"),
    ("Quels sont les objectifs du sous-processus de gestion des changements logiciels selon le DO-178C "
     "pour les niveaux logiciels C et D ?", ["QP2084"], "fr", "positive", "AS"),
    ("Dans le processus d'utilisation des ordres de fabrication, quels services sont responsables des impressions "
     "selon le type d'OF ?", ["P0055MI"], "fr", "positive", "AS"),
    ("Dans un système d'étanchéité pressurisé, quelles sont les applications prévues pour ces gorges "
     "et pourquoi ne sont-elles pas recommandées en usage dynamique ?", ["DTAS00009"], "fr", "positive", "AS"),
    ("Pour le processus de test COHV du système d'interconnexion, quelle commande client correspond au MSN 10208 "
     "et quel est son statut ?", ["FDAQL2568"], "fr", "positive", "IS"),
    ("Dans la saisie de schémas avec See Electrical Expert, où sont localisées les deux fiches du harnais 01 "
     "reliant deux tiroirs de la baie de localisation 100 ?", ["INE012"], "fr", "positive", "IS"),
    ("Dans le processus Stocker, quel est le périmètre d'application du document et quel niveau "
     "de confidentialité lui est attribué ?", ["IQ22223"], "fr", "positive", "IS"),
    # Promoted from missed_terms_baseline (codes missed by the baseline index, 2026-10-29)
    ("Dans quel document trouve-t-on la référence 21.A.249 ?", ["MR1020"], "fr", "positive", "ALL"),
    ("Which document refers to 21.A.249?", ["MR1020"], "en", "positive", "ALL"),
    ("Which document mentions ASNA2797?", ["MI14002"], "en", "positive", "ALL"),
    ("Quels documents citent la norme ASNA4187 ?", ["P0367MR"], "fr", "positive", "ALL"),
    ("Dans quel document trouve-t-on la norme ASNA3045 ?", ["IF20405"], "fr", "positive", "ALL"),
    ("Which document refers to BPS4453?", ["IC005"], "en", "positive", "ALL"),
    ("Which documents cite BAPS176?", ["IC032", "IP050"], "en", "positive", "ALL"),
    ("Quels documents font référence à la CS25853 ?", ["QTP01159", "QTR01159"], "fr", "positive", "ALL"),
    ("Which documents refer to ISO4287?", ["IC016", "MI14066", "MIT1135"], "en", "positive", "ALL"),
    ("Dans quel document est cité le LN1337 ?", ["U0019GO", "UDB700395"], "fr", "positive", "ALL"),
    ("Which documents mention LN1337?", ["U0019GO", "UDB700395"], "en", "positive", "ALL"),
    ("Which document mentions UDB7001076?", ["BEP005"], "en", "positive", "ALL"),
    ("Quel document cite l'IQ24_712 ?", ["PRLAT105"], "fr", "positive", "ALL"),
    ("Which document cites IQ24_712?", ["PRLAT105"], "en", "positive", "ALL"),
    ("Dans quel document est cité le VA-047 ?", ["FORM568"], "fr", "positive", "ALL"),
    ("Which documents refer to ATA28?", ["MI1303", "PRLAT525"], "en", "positive", "ALL"),
    ("Quels documents citent la norme AMS5516 ?", ["E0076MM", "Q0666MI"], "fr", "positive", "ALL"),
    ("Which document mentions MS172325?", ["MI13996"], "en", "positive", "ALL"),
    ("Quels documents citent la Q0712QR ?", ["Q0674MI"], "fr", "positive", "ALL"),
    ("Peux tu m'expliquer comment me connecter au MES ?", ["GO1614"], "fr", "positive", "ALL"),
    ("Who has the authorization to change a work order ?", ["P0055MI"], "en", "positive", "ALL"),
    ("comment livrer une pièce non finie ?", ["Q0441F"], "fr", "positive", "ALL"),
    ("Est ce que je peux livrer une pièce", ["Q0441F"], "fr", "positive", "ALL"),
    ("Comment modifier une gamme dans PLM ?", ["MI13793"], "fr", "positive", "ALL"),
    # Promoted from dataset (2026-11-02)
    ("Comment dois-je faire pour réparer une carte relais sur un testeur électrique LATE4000 ?",
     [], "fr", "to_complete", "IS"),
    ("Dans quel document puis-je trouver une aide sur les analyses E.F. non linéaires sur Nastran ?",
     ["E0026GO"], "fr", "positive", "AS"),
    ("J'ai besoin de la liste des fournisseurs du service informatique et cybersécurité",
     ["QP62523"], "fr", "positive", "ALL"),
    ("Je souhaite faire une analyse de risque sur une machine de marquage laser, "
     "saurais-tu m'indiquer la méthodologie ?",
     ["PRLAT508"], "fr", "positive", "IS"),
    ("L'APQP Leader est responsable d'« assurer la cascade finale des exigences client au fournisseur "
     "et la bonne application ». Dans quel § de la QP2178 est-ce écrit ?",
     ["QP2178"], "fr", "positive", "ALL"),
    ("Latécoère est-il conforme à l'exigence Dassault suivante, et quel document y répond : "
     "une confirmation de commande doit être systématiquement demandée aux fournisseurs ou sous-traitants, "
     "et les écarts analysés par le personnel compétent ?",
     [], "fr", "to_complete", "AS"),
    ("Quelle est la fréquence des audits procédés spéciaux pour le renouvellement "
     "de la qualification procédés spéciaux ?",
     ["PRLAT537"], "fr", "positive", "IS"),
    ("Quelles sont les règles de gestion des magasins ?", ["MI1331"], "fr", "positive", "AS"),
    ("Retrouve la FDAQL2914", ["FDAQL2914"], "fr", "positive", "ALL"),
    ("What is in DGL-1078_GB \u2013 Latecoere IT Charter (English version)?",
     ["DGL1078"], "en", "positive", "ALL"),
    ("When should I call an FAI when creating a new routine in the PLM?",
     ["MI13793"], "en", "positive", "ALL"),
]


KINDS = ["positive", "no_document", "to_complete"]
DIVISIONS = ["ALL", "AS", "IS"]
GOLDEN_SCHEMA = "question string, expected_refs array<string>, lang string, kind string, division string"


def canon_ref_col(col: Column) -> Column:
    """Canonical REF: upper case, letters and digits only (dash, space, underscore removed).

    :param col: REF column
    :return: the canonical REF column
    """
    return f.regexp_replace(f.upper(col), "[^A-Z0-9]", "")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs - Golden set

# COMMAND ----------

bad_rows = [row[0] for row in GOLDEN_SET if row[3] not in KINDS or row[4].upper() not in DIVISIONS
            or (row[3] == "positive") != bool(row[1])]
if bad_rows:
    raise ValueError(f"GOLDEN_SET rows with a wrong kind, division or expected REF: {bad_rows}")
duplicates = sorted({q for q in (row[0].strip().lower() for row in GOLDEN_SET)
                     if [r[0].strip().lower() for r in GOLDEN_SET].count(q) > 1})
if duplicates:
    raise ValueError(f"GOLDEN_SET questions written twice: {duplicates}")

df_golden_raw = spark.createDataFrame(GOLDEN_SET, GOLDEN_SCHEMA)
log.info(f"{df_golden_raw.count()} questions in GOLDEN_SET")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs - Indexed REF

# COMMAND ----------

df_indexed_refs = (
    spark.read.table(CHUNKS_TABLE)
    .where(f.col("REF").isNotNull())
    .select(canon_ref_col(f.col("REF")).alias("indexed_ref"))
    .dropDuplicates()
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - Canonical REF and stable question id
# MAGIC The id depends only on the text of the question: it stays the same from one run to the next, so a question can be
# MAGIC followed across the evaluation runs.

# COMMAND ----------

df_golden_prep = (
    df_golden_raw
    .withColumn("question", f.trim("question"))
    .withColumn("question_id", f.substring(f.sha2(f.lower(f.col("question")), 256), 1, 16))
    .withColumn("expected_refs", f.array_distinct(f.transform("expected_refs", canon_ref_col)))
    .withColumn("division", f.upper("division"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Flag the expected REF absent from the index
# MAGIC A REF is found when an indexed REF starts with it (the index carries language suffixes: `PRLAT538_FR`, `PRLAT538_EN`).

# COMMAND ----------

df_ref_check = (
    df_golden_prep.select("question_id", f.explode("expected_refs").alias("expected_ref"))
    .join(df_indexed_refs, f.col("indexed_ref").startswith(f.col("expected_ref")), "left")
    .groupBy("question_id", "expected_ref")
    .agg(f.max(f.col("indexed_ref").isNotNull()).alias("is_indexed"))
)

df_unknown_refs = (
    df_ref_check.where(~f.col("is_indexed"))
    .groupBy("question_id")
    .agg(f.array_sort(f.collect_list("expected_ref")).alias("unknown_refs"))
)

df_golden_final = (
    df_golden_prep.join(df_unknown_refs, "question_id", "left")
    .withColumn("unknown_refs", f.coalesce("unknown_refs", f.array().cast("array<string>")))
    .withColumn("updated_at", f.current_timestamp())
    .select("question_id", "question", "expected_refs", "unknown_refs", "division", "lang", "kind", "updated_at")
)

if DEBUG:
    display(df_golden_final)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write the golden set
# MAGIC Full overwrite: the table is always the exact image of `GOLDEN_SET`.

# COMMAND ----------

table_utils.save_table(
    OUTPUT_TABLE,
    df_golden_final,
    "overwrite",
    overwrite_schema=True,
)
spark.sql(f"COMMENT ON TABLE {OUTPUT_TABLE} IS 'Qualibot golden retrieval set: question -> expected REF. "
          f"Written by utils/evaluation/create_golden_retrieval_set.py, do not edit by hand.'")
log.info(f"{df_golden_final.count()} questions written to {OUTPUT_TABLE}")