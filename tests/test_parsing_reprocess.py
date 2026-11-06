"""run_mode=reprocess: only 3c_chunks writes (the code columns), the tasks before it and 4_describe_images stop at once."""

from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent / "utils" / "parsing_pipeline"
SKIPPING_NOTEBOOKS = (
    "create_proj_category_reference.py",
    "create_proj_parse_manifest.py",
    "create_proj_parsing_checkpoint.py",
    "create_proj_image_metadata.py",
)


def test_tasks_before_and_after_the_chunks_do_nothing_in_reprocess() -> None:
    """Every task that parses, prunes or describes exits on reprocess, before its first write."""
    for name in SKIPPING_NOTEBOOKS:
        source = (PIPELINE / name).read_text(encoding="utf-8")
        exit_at = source.index('dbutils.notebook.exit("reprocess")')
        assert source.index('== "reprocess"') < exit_at, name
        for first_work in ("setup_workers(", "spark.read", "write_table(", "describe_steps.", "categories.build"):
            if first_work in source:
                assert exit_at < source.index(first_work, source.index("# MAGIC ## Run mode")), (name, first_work)


def test_parse_task_sends_no_pdf_to_the_gpu_in_reprocess() -> None:
    """The GPU condition reads pdfs_for_gpu: 3_parse sets it to 0 before it exits."""
    source = (PIPELINE / "create_proj_parsing_checkpoint.py").read_text(encoding="utf-8")
    cell = source[source.index('== "reprocess"'):source.index('dbutils.notebook.exit("reprocess")')]
    assert 'taskValues.set(key="pdfs_for_gpu", value=0)' in cell


def test_chunks_task_reprocesses_before_reading_the_run_scope() -> None:
    """3c_chunks rewrites the code columns and exits before the scope 3_parse did not write."""
    source = (PIPELINE / "create_proj_chunks.py").read_text(encoding="utf-8")
    assert source.index("output_steps.reprocess_codes(") < source.index("parse_steps.read_run_scope()")
    assert source.index('dbutils.notebook.exit("reprocess")') < source.index("parse_steps.read_run_scope()")
