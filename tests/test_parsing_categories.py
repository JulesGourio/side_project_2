"""Category hierarchy, division reference and scope gate read from the Intraqual data asset tables (local Spark
session, skipped when pyspark or a JVM is missing)."""
import os
import shutil
import sys

import pytest

PARSING = os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline")
sys.path.insert(0, PARSING)
for key, value in {"PIPELINE_WRITE_ENV": "dev", "PARSING_INTRAQUAL_GOLD": "gold.sch",
                   "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
                   "PARSING_OFFLINE_MODELS": "/models"}.items():
    os.environ.setdefault(key, value)

pyspark = pytest.importorskip("pyspark")

import categories

REVISIONS = "test_categories_revisions"
ANCESTORS = "test_categories_ancestors"
REFERENCE = "test_categories_reference"

# (name, parent) by category id; the roots are the ids the division rules know.
CATEGORIES = {
    3346: ("AS - Aerostructures", None), 4000: ("2 - Qualite", 3346), 4001: ("Procedures", 4000),
    12: ("1 - PROCESSES / PROCESSUS", None), 13: ("Achats", 12),
    3284: ("IS - Interconnection Systems", None), 3285: ("Cablage", 3284),
    962: ("0 - DIRECTIVE GROUPE LATECOERE", None),
    900: ("Autre racine", None),
    3870: ("00-LATECOERE PROCESSES_One QMS", None), 5001: ("R30-Develop", 3870), 5002: ("AS", 5001),
    5003: ("IS", 5001), 5004: ("5- Quality manufacturing process", 5001), 5005: ("BUS", 5001),
}


def _path(idcat):
    """Category ids from the root down to ``idcat``."""
    path = [idcat]
    while CATEGORIES[path[0]][1] is not None:
        path.insert(0, CATEGORIES[path[0]][1])
    return path


@pytest.fixture(scope="module")
def spark():
    if shutil.which("java") is None:
        pytest.skip("no JVM")
    from pyspark.sql import SparkSession

    session = (SparkSession.builder.master("local[1]").appName("test_parsing_categories")
               .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "1")
               .config("spark.driver.bindAddress", "127.0.0.1").getOrCreate())
    # (revision id, reference, title, index, status, lifecycle, hidden, main category)
    revisions = [
        (1, "QP-1", "As doc", "B", 1, 7, False, 4001),
        (2, "QP-2", "Process doc", "A", 1, 7, False, 13),
        (3, "QP-3", "Is doc", "A", 1, 7, False, 3285),
        (4, "QP-4", "Directive", "A", 1, 7, False, 962),
        (5, "QP-5", "Other", "A", 1, 7, False, 900),
        (6, "QP-1", "As doc", "A", 0, 7, False, 4001),      # superseded revision
        (7, "QP-6", "Draft", "A", 1, 3, False, 13),         # not validated
        (8, "QP-7", "Hidden", "A", 1, 7, True, 13),         # hidden
        (9, "QP-8", "No category", "A", 1, 7, False, None),
        (10, "E-1", "Marked AS", "A", 1, 7, False, 5002),
        (11, "E-2", "Marked IS", "A", 1, 7, False, 5003),
        (12, "E-3", "Unmarked process", "A", 1, 7, False, 5004),
        (14, "E-5", "Marked BUS", "A", 1, 7, False, 5005),
    ]
    rows = [(i, ref, title, idx, status, state, hidden, cat,
             _path(cat) if cat else None, _path(cat)[0] if cat else None)
            for i, ref, title, idx, status, state, hidden, cat in revisions]
    session.createDataFrame(rows, "document_revision_ID long, document_reference string, document_title string, "
                                  "document_index string, revision_status_code long, lifecycle_state_code long, "
                                  "is_hidden boolean, category_ID long, category_ID_path array<long>, "
                                  "root_category_ID long").write.mode("overwrite").saveAsTable(REVISIONS)
    session.createDataFrame([(c, c, n) for c, (n, _) in CATEGORIES.items()],
                            "category_ID long, ancestor_category_ID long, ancestor_category_name string"
                            ).write.mode("overwrite").saveAsTable(ANCESTORS)
    yield session
    session.sql(f"DROP TABLE IF EXISTS {REVISIONS}")
    session.sql(f"DROP TABLE IF EXISTS {ANCESTORS}")
    shutil.rmtree("spark-warehouse", ignore_errors=True)


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    monkeypatch.setattr(categories, "INTRAQUAL_REVISIONS_TABLE", REVISIONS)
    monkeypatch.setattr(categories, "INTRAQUAL_CATEGORY_ANCESTORS_TABLE", ANCESTORS)


def _reference(spark):
    return {r.IDDOC: r.asDict() for r in categories.build_division_reference(spark).collect()}


def test_division_and_levels_of_each_root(spark):
    ref = _reference(spark)
    assert ref[1]["division"] == "AS"
    assert (ref[1]["niveau_plus_1"], ref[1]["niveau_plus_2"]) == ("AS - Qualite", "Procedures")
    assert ref[2]["division"] == "AS"
    assert (ref[2]["niveau_plus_1"], ref[2]["niveau_plus_2"]) == ("AS - PROCESSES / PROCESSUS", "Achats")
    assert ref[3]["division"] == "IS"
    assert (ref[3]["niveau_plus_1"], ref[3]["niveau_plus_2"]) == ("IS - Interconnection Systems", "Cablage")
    assert ref[4]["division"] == "DIRECTIVE"
    assert ref[4]["niveau_plus_1"] == "0 - DIRECTIVE GROUPE LATECOERE"
    assert ref[5]["division"] == "ALL"
    assert ref[5]["niveau_plus_1"] == "Autre racine"


def test_one_row_per_revision_with_a_category(spark):
    ref = _reference(spark)
    assert 9 not in ref
    assert len(ref) == 12
    assert {r["division_source"] for r in ref.values()} == {"live_hierarchy"}


def test_levels_below_the_category_depth_are_null(spark):
    ref = _reference(spark)
    assert ref[4]["niveau_plus_2"] is None
    assert ref[1]["niveau_plus_3"] is None


def test_a_leaf_category_named_after_a_division_sets_the_division(spark):
    ref = _reference(spark)
    assert ref[10]["division"] == "AS"
    assert ref[11]["division"] == "IS"
    assert ref[12]["division"] == "ALL"
    assert ref[14]["division"] == "ALL"
    assert ref[5]["division"] == "ALL"


def test_the_process_tree_is_exempt_from_the_date_filter(spark, monkeypatch):
    import datetime

    ref = _reference(spark)
    assert ref[10]["date_exempt"] and ref[12]["date_exempt"] and not ref[1]["date_exempt"]

    spark.createDataFrame([(r["IDDOC"], r["date_exempt"]) for r in ref.values()], "IDDOC long, date_exempt boolean"
                          ).write.mode("overwrite").saveAsTable(REFERENCE)
    monkeypatch.setattr(categories, "DIVISION_REFERENCE_TABLE", REFERENCE)
    old, recent = datetime.date(2010, 1, 1), datetime.date(2024, 1, 1)
    rows = [(1, old), (1 + 100, recent), (10, old), (12, recent), (99, None)]
    flagged = {r.IDDOC: r.is_archive for r in categories.with_archive_flag(
        spark, spark.createDataFrame(rows, "IDDOC long, doc_date date")).collect()}
    # 1: AS tree, before the cutoff -> archive; 10, 12: process tree -> indexed whatever the date; 101 and 99: no match, recent or undated.
    assert flagged == {1: True, 101: False, 10: False, 12: False, 99: False}
    spark.sql(f"DROP TABLE IF EXISTS {REFERENCE}")


def test_scope_keeps_current_validated_visible_revisions(spark):
    scope = {r.IDDOC: (r.ref, r.titre) for r in categories.load_scope_docs(spark).collect()}
    assert sorted(scope) == [1, 2, 3, 4, 5, 9, 10, 11, 12, 14]
    assert scope[1] == ("QP-1", "As doc")
