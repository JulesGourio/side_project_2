# Databricks notebook source
# MAGIC %md
# MAGIC # Inventaire des filtres, UAT (lecture seule)
# MAGIC Droits de Jules suffisants (SELECT sur `uat_proj.qualibot`). Pas besoin du job runner. Aucune ecriture, l'index n'est pas touche.
# MAGIC Colle les sorties des cellules 1 a 5 dans le fil.

# COMMAND ----------

# Cellule 1 : colonnes de la table source de l'index + volumes
CHUNKS = "uat_proj.qualibot.chunks"
ARCHIVE = "uat_proj.qualibot.chunks_archive"
spark.sql(f"DESCRIBE TABLE {CHUNKS}").show(60, False)
for t in (CHUNKS, ARCHIVE):
    try:
        print(t, spark.table(t).count())
    except Exception as e:
        print(t, "indisponible:", str(e)[:120])

# COMMAND ----------

# Cellule 2 : division (le seul filtre de l'UI) et date
spark.sql(f"""
SELECT division, COUNT(DISTINCT IDDOC) docs, COUNT(*) chunks,
       SUM(CASE WHEN doc_date IS NULL THEN 1 ELSE 0 END) chunks_sans_date,
       MIN(doc_date) date_min, MAX(doc_date) date_max
FROM {CHUNKS} GROUP BY division ORDER BY docs DESC""").show(50, False)

spark.sql(f"""
SELECT year(doc_date) annee, COUNT(DISTINCT IDDOC) docs
FROM {CHUNKS} GROUP BY 1 ORDER BY 1""").show(60, False)

# COMMAND ----------

# Cellule 3 : categories (niveau_plus_*, dispo dans chunks ? sinon voir cellule 1)
cols = [r.col_name for r in spark.sql(f"DESCRIBE TABLE {CHUNKS}").collect()]
print([c for c in cols if c.startswith("niveau_plus")] or "pas de niveau_plus_* dans chunks")
for c in ("niveau_plus_1", "niveau_plus_2"):
    if c in cols:
        spark.sql(f"""SELECT division, {c}, COUNT(DISTINCT IDDOC) docs
                      FROM {CHUNKS} GROUP BY 1,2 ORDER BY docs DESC""").show(80, False)

# COMMAND ----------

# Cellule 4 : autres metadonnees filtrables
for c in ("type_document", "langue", "chunk_content_type"):
    if c in cols:
        spark.sql(f"""SELECT {c}, COUNT(DISTINCT IDDOC) docs, COUNT(*) chunks
                      FROM {CHUNKS} GROUP BY 1 ORDER BY docs DESC""").show(40, False)

# COMMAND ----------

# Cellule 5 : colonnes reellement exposees par l'index (lecture des metadonnees, pas de requete d'ecriture)
from databricks.vector_search.client import VectorSearchClient
idx = VectorSearchClient(disable_notice=True).get_index(index_name="uat_proj.qualibot.chunks_index")
d = idx.describe()
print({k: d.get(k) for k in ("name", "endpoint_name", "primary_key", "index_type")})
print(d.get("delta_sync_index_spec", {}).get("source_table"), d.get("delta_sync_index_spec", {}).get("columns_to_sync"))
