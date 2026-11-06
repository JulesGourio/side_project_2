# Verification du run reprocess (lecture seule). Une cellule, tes droits suffisent (SELECT sur prod_proj).
# Compare chunks a la version juste avant le MERGE du reprocess : seules chunk_preprocessed et cited_codes doivent changer.
from pyspark.sql import functions as F

ENV = "prod"
TABLES = [f"{ENV}_proj.qualibot.chunks", f"{ENV}_proj.qualibot.chunks_archive"]
BRACKET = r" \[[A-Z0-9]+\]"

for table in TABLES:
    print(f"\n==================== {table}")
    hist = spark.sql(f"DESCRIBE HISTORY {table} LIMIT 5").collect()
    last = hist[0]
    print(f"Derniere operation : version {last.version} {last.operation} {last.timestamp}")
    if last.operation != "MERGE":
        print("Pas de MERGE en derniere position (0 passage modifie, ou une autre ecriture a suivi) : rien a comparer.")
        continue
    before = last.version - 1
    print(f"Lignes mises a jour (MERGE) : {last.operationMetrics.get('numTargetRowsUpdated')} | "
          f"retour arriere : RESTORE TABLE {table} TO VERSION AS OF {before}")

    new = spark.table(table)
    old = spark.read.option("versionAsOf", before).table(table)
    cols = [c for c in new.columns if c not in ("chunk_preprocessed", "cited_codes")]
    j = old.alias("o").join(new.alias("n"), "chunk_id", "full_outer")
    other_changed = F.lit(False)
    for c in cols:
        if c != "chunk_id":
            other_changed = other_changed | ~F.col(f"o.{c}").eqNullSafe(F.col(f"n.{c}"))
    strip = lambda col: F.regexp_replace(F.coalesce(col, F.lit("")), BRACKET, "")
    r = j.select(
        F.count("*").alias("lignes"),
        F.sum(F.col("o.chunk_text").isNull().cast("int")).alias("absentes_avant"),
        F.sum(F.col("n.chunk_text").isNull().cast("int")).alias("absentes_apres"),
        F.sum(other_changed.cast("int")).alias("autres_colonnes_changees"),
        F.sum((~F.col("o.chunk_preprocessed").eqNullSafe(F.col("n.chunk_preprocessed"))).cast("int")).alias("pre_change"),
        F.sum((~F.col("o.cited_codes").eqNullSafe(F.col("n.cited_codes"))).cast("int")).alias("cited_change"),
        F.sum((F.col("n.chunk_preprocessed").isNull() | F.col("n.cited_codes").isNull()).cast("int")).alias("nulls_apres"),
        F.sum((strip(F.col("n.chunk_preprocessed")) != strip(F.col("n.chunk_text"))).cast("int")).alias("texte_altere"),
        F.sum((F.size(F.split(F.trim(F.col("o.cited_codes")), " ")) * (F.trim(F.col("o.cited_codes")) != "").cast("int"))).alias("codes_cites_avant"),
        F.sum((F.size(F.split(F.trim(F.col("n.cited_codes")), " ")) * (F.trim(F.col("n.cited_codes")) != "").cast("int"))).alias("codes_cites_apres"),
    ).first().asDict()
    for k, v in r.items():
        print(f"  {k:26s} {v}")
    ok = (r["absentes_avant"] == 0 and r["absentes_apres"] == 0 and r["autres_colonnes_changees"] == 0
          and r["nulls_apres"] == 0 and r["texte_altere"] == 0)
    print("  => OK : memes lignes, memes textes, seules les 2 colonnes de codes changent" if ok
          else "  => A REGARDER : une des valeurs absentes/autres/nulls/texte_altere n'est pas 0")

    if table.endswith(".chunks"):
        codes = lambda df, v: (df.select("chunk_id", F.explode(F.split(F.trim("cited_codes"), " ")).alias("code"))
                               .filter("code != ''").withColumn("v", F.lit(v)))
        diff = codes(old, "avant").unionByName(codes(new, "apres"))
        per = diff.groupBy("chunk_id", "code").agg(F.collect_set("v").alias("vs"))
        print("\nCodes perdus (dans cited_codes avant, plus apres) : top 30")
        display(per.filter(F.size("vs") == 1).filter(F.array_contains("vs", "avant"))
                .groupBy("code").count().orderBy(F.desc("count")).limit(30))
        print("Codes gagnes : top 30")
        display(per.filter(F.size("vs") == 1).filter(F.array_contains("vs", "apres"))
                .groupBy("code").count().orderBy(F.desc("count")).limit(30))
        print("Exemple : passages qui contiennent [IQ253851]")
        display(new.filter(F.col("chunk_preprocessed").contains("[IQ253851]"))
                .select("chunk_id", "REF", F.substring("chunk_preprocessed", 1, 300).alias("extrait"), "cited_codes")
                .limit(10))
