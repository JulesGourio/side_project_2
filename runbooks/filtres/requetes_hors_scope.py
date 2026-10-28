# Databricks notebook source
# MAGIC %md
# MAGIC # Documents "dans le scope" filtres par date ou categorie (lecture seule)
# MAGIC Tes droits suffisent (SELECT sur `prod_gold.quality_system` et `uat_proj.qualibot`). Rien n'est ecrit.
# MAGIC "Scope" = courant, etat 7, non masque. Colle les sorties des cellules.

# COMMAND ----------

R = "prod_gold.quality_system.document_revisions_intraqual"
A = "prod_gold.quality_system.category_ancestors_intraqual"
U = "uat_proj.qualibot"
BASE = "revision_status_code = 1 AND lifecycle_state_code = 7 AND is_hidden = false"
IN_CAT = "(root_category_ID IN (3870,948,3209,12,962) OR exists(category_ID_path, x -> x IN (3967,4006)))"

# COMMAND ----------

# 1. Vue d'ensemble : documents vivants par racine, dans le perimetre ou non
spark.sql(f"""
SELECT root_category_ID, n.nom AS racine,
       {IN_CAT} AS dans_perimetre_categories, COUNT(*) docs
FROM {R} r
LEFT JOIN (SELECT ancestor_category_ID id, first(ancestor_category_name) nom FROM {A} GROUP BY 1) n ON n.id = r.root_category_ID
WHERE {BASE}
GROUP BY 1,2,3 ORDER BY docs DESC""").show(100, False)

# COMMAND ----------

# 2. Filtres PAR CATEGORIE : vivants hors perimetre, par categorie principale (top 100)
spark.sql(f"""
SELECT root_category_ID, category_ID, count(*) docs, min(published_date) plus_ancien, max(published_date) plus_recent
FROM {R} WHERE {BASE} AND NOT {IN_CAT}
GROUP BY 1,2 ORDER BY docs DESC LIMIT 100""").show(100, False)

# COMMAND ----------

# 3. Detail hors perimetre pour les grosses categories : exemples (REF, titre, date)
spark.sql(f"""
SELECT root_category_ID, category_ID, document_reference ref, document_title titre, published_date
FROM {R} WHERE {BASE} AND NOT {IN_CAT}
ORDER BY published_date DESC LIMIT 200""").show(200, False)

# COMMAND ----------

# 4. Filtres PAR DATE : dans le perimetre categories mais publies avant 2016-01-01
spark.sql(f"""
SELECT year(published_date) annee, COUNT(*) docs
FROM {R} WHERE {BASE} AND {IN_CAT} AND published_date < DATE'2016-01-01'
GROUP BY 1 ORDER BY 1 DESC""").show(40, False)

spark.sql(f"""
SELECT root_category_ID, COUNT(*) docs
FROM {R} WHERE {BASE} AND {IN_CAT} AND published_date < DATE'2016-01-01'
GROUP BY 1 ORDER BY docs DESC""").show(20, False)

# COMMAND ----------

# 5. Exemples avant 2016 (REF, titre, date), les plus recents d'abord
spark.sql(f"""
SELECT root_category_ID, document_reference ref, document_title titre, published_date
FROM {R} WHERE {BASE} AND {IN_CAT} AND published_date < DATE'2016-01-01'
ORDER BY published_date DESC LIMIT 200""").show(200, False)

# COMMAND ----------

# 6. Date inconnue (compte comme recent, donc parse)
spark.sql(f"SELECT COUNT(*) sans_date FROM {R} WHERE {BASE} AND {IN_CAT} AND published_date IS NULL").show()

# COMMAND ----------

# 7. Documents "non courants / non diffuses / masques" qui seraient dans les categories (le filtre de statut)
spark.sql(f"""
SELECT revision_status_code, lifecycle_state_code, is_hidden, COUNT(*) docs
FROM {R} WHERE {IN_CAT}
GROUP BY 1,2,3 ORDER BY docs DESC""").show(50, False)

# COMMAND ----------

# 8. Dans le scope complet (statut + categories + >= 2016) mais absents de parsed_documents UAT
spark.sql(f"""
SELECT r.root_category_ID, COUNT(*) docs_manquants
FROM {R} r LEFT ANTI JOIN {U}.parsed_documents d ON d.IDDOC = r.document_revision_ID
WHERE {BASE} AND {IN_CAT} AND (r.published_date IS NULL OR r.published_date >= DATE'2016-01-01')
GROUP BY 1 ORDER BY 2 DESC""").show(20, False)

# COMMAND ----------

# 9. Exclusions manuelles et statuts SKIPPED dans UAT
spark.sql(f"SELECT reason, COUNT(*) n FROM {U}.rag_exclusions GROUP BY 1 ORDER BY 2 DESC").show(20, False)
spark.sql(f"SELECT * FROM {U}.rag_exclusions LIMIT 50").show(50, False)
