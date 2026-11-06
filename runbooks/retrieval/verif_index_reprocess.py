# Verification de l'index apres reprocess (lecture seule). Une cellule, cluster classique PROD, tes droits suffisent.
# Compare, pour 20 passages modifies par le reprocess, chunk_preprocessed dans la table et dans l'index.
import json
from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

TABLE = "prod_proj.qualibot.chunks"
INDEX = "prod_proj.qualibot.chunks_index"
AVANT = 56  # version de chunks juste avant le MERGE du reprocess

w = WorkspaceClient()
st = w.vector_search_indexes.get_index(index_name=INDEX).status
print(f"Etat index : {st.detailed_state} | ready={st.ready} | lignes indexees={st.indexed_row_count}")
print(f"Message : {st.message}")

old = spark.read.option("versionAsOf", AVANT).table(TABLE).select("chunk_id", F.col("chunk_preprocessed").alias("avant"))
new = spark.table(TABLE).select("chunk_id", F.col("chunk_preprocessed").alias("apres"))
changed = (old.join(new, "chunk_id").filter(~F.col("avant").eqNullSafe(F.col("apres")))
              .orderBy(F.rand(7)).limit(20).collect())

a_jour = ancien = absent = 0
for r in changed:
    res = w.vector_search_indexes.query_index(
        index_name=INDEX, columns=["chunk_id", "chunk_preprocessed"], query_text="code",
        filters_json=json.dumps({"chunk_id": r.chunk_id}), num_results=1)
    rows = (res.result.data_array or []) if res.result else []
    if not rows:
        absent += 1
        print(f"{r.chunk_id} : absent de l'index")
        continue
    dans_index = rows[0][1]
    if dans_index == r.apres:
        a_jour += 1
    elif dans_index == r.avant:
        ancien += 1
        print(f"{r.chunk_id} : l'index a encore l'ancienne version")
    else:
        print(f"{r.chunk_id} : l'index a une autre version")
print(f"\nSur {len(changed)} passages modifies : {a_jour} a jour dans l'index, {ancien} encore anciens, {absent} absents")
print("=> INDEX A JOUR" if a_jour == len(changed) else "=> INDEX PAS ENCORE A JOUR (synchro en cours ou pas lancee)")
