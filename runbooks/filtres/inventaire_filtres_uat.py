# Databricks notebook source
# MAGIC %md
# MAGIC # Filtres du parsing, UAT (lecture seule)
# MAGIC Tes droits suffisent (SELECT sur `uat_proj.qualibot`), pas besoin du job runner. Rien n'est ecrit, l'index n'est pas touche.
# MAGIC Colle les sorties des cellules 1 a 5.

# COMMAND ----------

# Cellule 1 : colonnes des tables clefs
P = "uat_proj.qualibot"
for t in ("parse_manifest", "parsed_documents", "category_reference"):
    print("==", t)
    spark.sql(f"DESCRIBE TABLE {P}.{t}").select("col_name", "data_type").show(60, False)

# COMMAND ----------

# Cellule 2 : decision du manifest par statut (ce qui est parse, exclu, pourquoi)
pm_cols = [r.col_name for r in spark.sql(f"DESCRIBE TABLE {P}.parse_manifest").collect()]
print(pm_cols)
for c in ("parse_status", "status", "parse_content", "is_archive"):
    if c in pm_cols:
        spark.sql(f"SELECT {c}, COUNT(*) docs FROM {P}.parse_manifest GROUP BY 1 ORDER BY 2 DESC").show(40, False)

# COMMAND ----------

# Cellule 3 : resultat du parsing (statut, RAG, date)
pd_cols = [r.col_name for r in spark.sql(f"DESCRIBE TABLE {P}.parsed_documents").collect()]
for c in ("status", "is_in_rag", "is_filtered_by_date"):
    if c in pd_cols:
        spark.sql(f"SELECT {c}, COUNT(*) docs FROM {P}.parsed_documents GROUP BY 1 ORDER BY 2 DESC").show(40, False)

# COMMAND ----------

# Cellule 4 : documents dates avant la coupure 2016-01-01, par annee
if "doc_date" in pm_cols:
    spark.sql(f"""SELECT year(doc_date) annee, COUNT(*) docs FROM {P}.parse_manifest
                  WHERE doc_date < DATE'2016-01-01' GROUP BY 1 ORDER BY 1 DESC""").show(30, False)
    spark.sql(f"SELECT COUNT(*) sans_date FROM {P}.parse_manifest WHERE doc_date IS NULL").show()

# COMMAND ----------

# Cellule 5 : categories des documents indexes (division x niveau 1 x niveau 2)
spark.sql(f"""
SELECT c.division, c.niveau_plus_1, c.niveau_plus_2, COUNT(DISTINCT d.IDDOC) docs
FROM {P}.parsed_documents d JOIN {P}.category_reference c USING (IDDOC)
GROUP BY 1,2,3 ORDER BY docs DESC""").show(100, False)
