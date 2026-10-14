"""PII cleaning utilities for bronze-to-bronze-cleaned pipelines.

Contains:
- PIITag enum and PIICleaningManager class for PII column discovery
- Individual cleaning/casting helper functions (one per step)
- Tag propagation utility
"""

from collections import defaultdict
from enum import Enum

import pyspark.sql.functions as f
from pyspark.sql import DataFrame, SparkSession

from leap_utils.common.logger import get_logger
from leap_utils.data_asset import table_utils

log = get_logger(__name__)

PII_TAG_PREFIX = (
    "abac_hr_pii"  # This is the PII tag prefix - all should begin with this substring
)

TYPES_BOOL = ("boolean", "bool")
TYPES_STR = ("string", "str")
TYPES_DATE = ("date", "timestamp", "datetime", "dateType")

# Default date formats tried in order by cast_columns_to_date.
# First non-null result wins (coalesce). Override by passing formats= explicitly.
# Pass formats=[] to force strict ISO-8601 parsing only.
DEFAULT_DATE_FORMATS: tuple[str] = (
    "yyyy-MM-dd",   # ISO 8601
    "dd/MM/yyyy",   # European
    "MM/dd/yyyy",   # US
    "dd-MM-yyyy",
    "yyyyMMdd",     # compact
)

# Mojibake detection: a 2-byte UTF-8 sequence that was misread as two Latin-1 characters.
# First byte (0xC0–0xDF) appears as U+00C0–U+00DF, second byte (0x80–0xBF) as U+0080–U+00BF.
# Covers all French accented characters in U+00C0–U+00FF (é, è, ê, à, â, ù, û, î, ô, ç, etc.)
_MOJIBAKE_RE = "[\u00C0-\u00DF][\u0080-\u00BF]"

# Table name prefix patterns indicating tables to be skipped
TABLES_FORBIDDEN_STARTWITH_PATTERNS = ("event_log_", "__")

class PIITag(Enum):
    """PII sensitivity levels used as Unity Catalog column tag prefixes."""

    STANDARD = f"{PII_TAG_PREFIX}_standard"
    CONFIDENTIAL = f"{PII_TAG_PREFIX}_confidential"
    RGPD = f"{PII_TAG_PREFIX}_rgpd"


class PIICleaningManager:
    """Discover and manage PII-tagged columns across a schema.

    Queries Unity Catalog column_tags to build an index of PII columns grouped
    by sensitivity tag and table name.  Provides helpers to load raw DataFrames
    and look up tagged columns for downstream cleaning pipelines.

    :param spark: Active SparkSession. Needed for SQL queries.
    :type spark: SparkSession
    :param schema: Schema containing the tables to process.
    :type schema: str
    :param catalog_source: Catalog where raw (uncleaned) tables live.
    :type catalog_source: str
    :param catalog_target: Catalog where cleaned tables will be written.
    :type catalog_target: str
    :param tables_to_load: Allowlist of table names to process.
    :type tables_to_load: list[str]
    :raises ValueError: If *catalog_source* equals *catalog_target*.
    """

    def __init__(
        self,
        spark: SparkSession,
        schema: str,
        catalog_source: str,
        catalog_target: str,
        tables_to_load: list[str]
    ):
        if catalog_source == catalog_target:
            raise ValueError("Source and target catalog must be different.")

        self.spark = spark
        self.catalog_source = catalog_source
        self.catalog_target = catalog_target
        self.schema = schema
        self.tables_to_load = tables_to_load

        # SQL filter for tables to process, used in following private methods
        self._sql_table_filter = ", ".join(f"'{t}'" for t in self.tables_to_load)

        # Build a nested index of PII-tagged columns grouped by tag pattern then table.
        self.pii_column_index: dict[PIITag, dict[str, list[str]]] = {}
        for tag in PIITag:
            self.pii_column_index.update(self._build_pii_tag_index(tag=tag))

        # Retreive schema columns, it is needed to propagate them to the cleaned
        # table_name: list of column names
        self.source_schema_columns = self._get_source_schema_columns()

        # Column mapping: {table_name: {source_column: target_column}}
        # Initialized as identity (source == target), updated by rename methods.
        self.column_mapping: dict[str, dict[str, str]] = {
            t: {c: c for c in cols}
            for t, cols in self.source_schema_columns.items()
        }

        # Retreive source tables and columns comments, it is needed to propagate them to the cleaned data
        self.source_tables_comments = self._get_source_tables_comments()
        self.source_columns_comments = self._get_source_columns_comments()
        

    def _build_pii_tag_index(self, tag: PIITag) -> dict[PIITag, dict[str, list[str]]]:
        """Build a nested index of PII-tagged columns grouped by tag pattern then table."""
        rows = self.spark.sql(f"""
            SELECT tag_name, table_name, column_name
            FROM system.information_schema.column_tags
            WHERE catalog_name = '{self.catalog_source}'
              AND schema_name  = '{self.schema}'
              AND tag_name     LIKE '%{tag.value}%'
              AND table_name   IN ({self._sql_table_filter})
            ORDER BY tag_name, table_name, column_name
        """).collect()

        index: dict[PIITag, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in rows:
            index[tag][row.table_name].append(row.column_name)

        return {tag_key: dict(tables) for tag_key, tables in index.items()}
    
    def _get_source_schema_columns(self) -> dict[str, list[str]]:
        """Return the full schema as {table_name: [column_name, ...]} for the source catalog/schema."""
        rows = self.spark.sql(f"""
            SELECT table_name, column_name
            FROM {self.catalog_source}.information_schema.columns
            WHERE table_schema = '{self.schema}'
              AND table_name   IN ({self._sql_table_filter})
            ORDER BY table_name, ordinal_position
        """).collect()

        schema_index: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            schema_index[row.table_name].append(row.column_name)
        return dict(schema_index)
    
    def _get_source_tables_comments(self) -> dict[str, str]:
        """Snapshot the source TABLE-level comments too — these are not carried by the DataFrame nor by
        saveAsTable, so they must be re-applied explicitly on the cleaned table (see Outputs)."""
        source_table_comments: dict[str, str] = {
            r["table_name"]: r["comment"]
            for r in self.spark.sql(
                f"SELECT table_name, comment FROM {self.catalog_source}.information_schema.tables "
                f"WHERE table_schema = '{self.schema}' AND table_name IN ({self._sql_table_filter})"
            ).collect()
            if r["comment"]
        }
        return source_table_comments
    
    def _get_source_columns_comments(self) -> dict[str, dict[str, str]]:
        """Snapshot the source column comments per table. Comments live in information_schema
        and are DROPPED when a column is rebuilt by cleaning (clean_string_columns / lower / cast).
        We re-attach them before the write (see Outputs) so saveAsTable persists them —
        comments "follow" the column, without needing UC lineage."""
        rows = self.spark.sql(f"""
            SELECT table_name, column_name, comment
            FROM {self.catalog_source}.information_schema.columns
            WHERE table_schema = '{self.schema}'
              AND table_name   IN ({self._sql_table_filter})
              AND comment IS NOT NULL
        """).collect()

        source_columns_comments: dict[str, dict[str, str]] = defaultdict(dict)
        for row in rows:
            source_columns_comments[row.table_name][row.column_name] = row.comment
        return dict(source_columns_comments)

    def _get_existing_target_tags(self) -> set[tuple[str, str, str]]:
        """Fetch PII tags already applied on the target catalog/schema.

        :return: ``(table_name, column_name, tag_name)`` tuples already present on target.
        :rtype: set[tuple[str, str, str]]
        """
        rows = self.spark.sql(f"""
            SELECT table_name, column_name, tag_name
            FROM system.information_schema.column_tags
            WHERE catalog_name = '{self.catalog_target}'
              AND schema_name  = '{self.schema}'
              AND table_name   IN ({self._sql_table_filter})
              AND tag_name     LIKE '{PII_TAG_PREFIX}%'
        """).collect()
        return {(r.table_name, r.column_name, r.tag_name) for r in rows}

    def get_target_schema_columns(self) -> dict[str, list[str]]:
        """Derive current target column names from column_mapping."""
        return {t: list(m.values()) for t, m in self.column_mapping.items()}

    def normalize_column_names(self, df: DataFrame, table_name: str, columns: list[str]) -> DataFrame:
        """Normalise PII column names: strip whitespace, lowercase, replace spaces with
        underscores, and strip trailing underscores. Updates self.column_mapping
        to reflect the new column names.

        The trailing-underscore strip targets sources (e.g. Kelio) where every column ends
        with ``_`` (matricule_ -> matricule). It is a no-op for sources whose columns do not
        end with ``_`` (tress / immat / horoquartz). Non-PII columns (e.g. surrogate PKs) are
        not passed here, so they keep their trailing underscore.
        """
        if table_name not in self.column_mapping:
            log.warning(f"Table {table_name} not found in schema {self.schema}")
            return df
        
        column_rename_map: dict[str, str] = {}
        for col in columns:
            column_rename_map[col] = col.strip().lower().replace(" ", "_").rstrip("_")
        log.debug(f"PII column rename map: {column_rename_map}")

        df_renamed = df.withColumnsRenamed(column_rename_map)

        # Update column_mapping: for each source_col whose current target is being renamed,
        # replace the target value with the new name.
        for source_col, target_col in self.column_mapping[table_name].items():
            if target_col in column_rename_map:
                self.column_mapping[table_name][source_col] = column_rename_map[target_col]

        return df_renamed
    
    def rename_columns(self, df: DataFrame, table_name: str, column_map: dict[str, str]) -> DataFrame:
        """Rename columns using an explicit mapping and update the internal column_mapping.

        :param df: DataFrame whose columns should be renamed.
        :type df: DataFrame
        :param table_name: Name of the table being processed.
        :type table_name: str
        :param column_map: Mapping of ``{old_column_name: new_column_name}``.
        :type column_map: dict[str, str]
        :return: DataFrame with columns renamed according to *column_map*.
        :rtype: DataFrame
        """
        if table_name not in self.column_mapping:
            log.warning(f"Table {table_name} not found in schema {self.schema}")
            return df

        log.debug(f"Column rename map: {column_map}")

        df_renamed = df
        for old_col, new_col in column_map.items():
            df_renamed = df_renamed.withColumnRenamed(old_col, new_col)

        # Update column_mapping: for each source_col whose current target is being renamed,
        # replace the target value with the new name.
        for source_col, target_col in self.column_mapping[table_name].items():
            if target_col in column_map:
                self.column_mapping[table_name][source_col] = column_map[target_col]

        return df_renamed

    def __repr__(self) -> str:
        """Pretty-print the PII column index grouped by tag and table."""
        lines = [
            f"PIICleaningManager({self.catalog_source} → {self.catalog_target} / {self.schema})"
        ]
        lines.append("=" * len(lines[0]))
        for tag, tables in self.pii_column_index.items():
            total_cols = sum(len(cols) for cols in tables.values())
            lines.append(f"\n ▸ {tag.name} ({len(tables)} tables, {total_cols} columns)")
            for table_name, cols in sorted(tables.items()):
                lines.append(f" - {table_name}:\n   {', '.join(cols)}")
        return "\n".join(lines)

    def get_pii_columns(self, table_name: str, tag: PIITag) -> list[str]:
        """Return the list of PII-tagged column names for a given table and tag level."""
        cols = self.pii_column_index[tag].get(table_name)
        return cols if cols is not None else []
    
    def load_all_by_tag(self) -> dict["PIITag | None", dict[str, DataFrame]]:
        """Load every table in the schema as a DataFrame, grouped by PII tag pattern."""
        all_tables_names: set[str] = {
            row.table_name
            for row in self.spark.sql(f"""
                SELECT table_name
                FROM {self.catalog_source}.information_schema.tables
                WHERE table_schema = '{self.schema}'
            """).collect()
            if not row.table_name.startswith(TABLES_FORBIDDEN_STARTWITH_PATTERNS)
        }

        tables_all: dict[str, DataFrame] = {
            table_name: self.spark.read.table(
                f"{self.catalog_source}.{self.schema}.{table_name}"
            )
            for table_name in all_tables_names
        }

        tagged_tables_names: set[str] = {
            table_name
            for tables in self.pii_column_index.values()
            for table_name in tables.keys()
        }

        result: dict["PIITag | None", dict[str, DataFrame]] = {
            tag_val: {
                table_name: tables_all[table_name]
                for table_name in tables
                if table_name in tables_all
            }
            for tag_val, tables in self.pii_column_index.items()
        }

        untagged = all_tables_names - tagged_tables_names
        if untagged:
            result[None] = {t: tables_all[t] for t in untagged}

        return result

    def propagate_pii_tags(self, simulate: bool = False) -> int:
        """Propagate PII column tags from source to target tables using column_mapping.

        Resolves target column names via self.column_mapping, then applies the source PII
        tag on the target catalog. Tags already present on the target are skipped to avoid
        redundant ALTER TABLE calls (idempotent, safe to re-run).

        :param simulate: When True, logs the tags that would be applied without executing DDL.
        :type simulate: bool
        :return: Number of tags actually applied (excludes already-tagged and skipped).
        :rtype: int
        """
        from pyspark.sql.utils import AnalysisException

        # Fetch tags already present on target — skip these to avoid redundant ALTERs
        already_tagged = self._get_existing_target_tags()

        # Build the target schema to validate column existence before ALTER
        target_columns = self.get_target_schema_columns()

        n_applied = 0
        n_skipped = 0
        n_already = 0

        for tag, tables in self.pii_column_index.items():
            for table_name, source_columns in tables.items():
                mapping = self.column_mapping.get(table_name, {})
                target_table = f"{self.catalog_target}.{self.schema}.{table_name}"
                valid_target_cols = set(target_columns.get(table_name, []))

                for source_col in source_columns:
                    target_col = mapping.get(source_col, source_col)

                    # Skip if tag already present on target
                    if (table_name, target_col, tag.value) in already_tagged:
                        n_already += 1
                        log.info(
                                f"Tag {target_table}.{target_col} ← {tag.value} "
                                f"(source: {source_col}) already present, skipping"
                            )
                        continue

                    # Skip if target column does not exist (stale lineage or dropped column)
                    if target_col not in valid_target_cols:
                        log.warning(
                            f"Target column {target_table}.{target_col} does not exist, "
                            f"skipping tag {tag.value} (source: {source_col})."
                        )
                        n_skipped += 1
                        continue

                    query = (
                        f"ALTER TABLE {target_table} "
                        f"ALTER COLUMN `{target_col}` "
                        f"SET TAGS ('{tag.value}' = '')"
                    )

                    if simulate:
                        log.info(f"[simulate] {query}")
                        n_applied += 1
                    else:
                        try:
                            self.spark.sql(query)
                        except AnalysisException as e:
                            log.warning(
                                f"Could not tag {target_table}.{target_col} ← {tag.value} "
                                f"(source: {source_col}): {e}"
                            )
                            n_skipped += 1
                        else:
                            log.info(
                                f"Tagged {target_table}.{target_col} ← {tag.value} "
                                f"(source: {source_col})"
                            )
                            n_applied += 1

        log.info(
            f"{'Would propagate' if simulate else 'Propagated'} {n_applied} PII tag(s) "
            f"from {self.catalog_source}.{self.schema} → {self.catalog_target}.{self.schema} "
            f"({n_already} already tagged, {n_skipped} skipped)."
        )
        return n_applied

    def propagate_columns_comments(self, table_name: str, df_clean: DataFrame) -> DataFrame:
        """Re-attach source column comments to the cleaned DataFrame via column_mapping.

        :param table_name: Name of the table being processed.
        :type table_name: str
        :param df_clean: Cleaned DataFrame to annotate with comments.
        :type df_clean: DataFrame
        :return: DataFrame with source column comments restored as metadata.
        :rtype: DataFrame
        """
        source_comments = self.source_columns_comments.get(table_name, {})
        column_map = self.column_mapping.get(table_name, {})
        columns = df_clean.columns
        for src_col, cmt in source_comments.items():
            target_col = column_map.get(src_col)
            if target_col in columns:
                df_clean = df_clean.withMetadata(target_col, {"comment": cmt})
        return df_clean

    def propagate_table_comments(
        self,
        table_name: str,
        df_clean: DataFrame,
        destination_table_full_path: str,
    ) -> None:
        """Apply source table comment to the target table if missing or different.

        :param table_name: Name of the table being processed.
        :type table_name: str
        :param df_clean: Cleaned DataFrame (unused, kept for interface consistency).
        :type df_clean: DataFrame
        :param destination_table_full_path: Fully qualified target table path.
        :type destination_table_full_path: str
        """
        source_comment = self.source_tables_comments.get(table_name)
        if source_comment:
            _row = self.spark.sql(
                f"SELECT comment FROM {self.catalog_target}.information_schema.tables "
                f"WHERE table_schema = '{self.schema}' AND table_name = '{table_name}'"
            ).first()
            if (_row["comment"] if _row else None) != source_comment:
                self.spark.sql(
                    f"COMMENT ON TABLE {destination_table_full_path} IS "
                    f"'{source_comment.replace(chr(39), chr(39) * 2)}'"
                )
                # rem: chr(39) is a single quote, so it must be doubled to escape it in SQL

    def set_primary_keys(
        self,
        table_name: str,
        df_clean: DataFrame,
        destination_table_full_path: str,
        primary_keys: dict[str, list[str]],
    ) -> None:
        """Set UC PK tags and PRIMARY KEY constraint if not already present.

        :param table_name: Name of the table being processed.
        :type table_name: str
        :param df_clean: Cleaned DataFrame (used to verify PK columns exist).
        :type df_clean: DataFrame
        :param destination_table_full_path: Fully qualified target table path.
        :type destination_table_full_path: str
        :param primary_keys: Mapping of table names to their PK column lists.
        :type primary_keys: dict[str, list[str]]
        """
        pk_cols = [c for c in primary_keys.get(table_name, []) if c in df_clean.columns]
        if pk_cols:
            constraint_name = f"{table_name}_pk"
            nb_cons = self.spark.sql(
                f"SELECT count(*) FROM system.information_schema.table_constraints "
                f"WHERE table_catalog = '{self.catalog_target}' "
                f"AND table_schema = '{self.schema}' "
                f"AND table_name = '{table_name}' AND constraint_name = '{constraint_name}'"
            ).first()[0]
            if nb_cons == 0:
                table_utils.apply_pk_fk_tags_uc(
                    full_table_name=destination_table_full_path,
                    primary_key=pk_cols,
                    foreign_keys=[],
                    dry_run=False,
                )
                for col in pk_cols:
                    table_utils.set_table_column_not_null(
                        table_path=destination_table_full_path, column=col
                    )
                table_utils.add_table_primary_key(
                    table_path=destination_table_full_path,
                    pk_name=constraint_name,
                    columns=",".join(pk_cols),
                )
                log.info(
                    f"Applied PK tags + constraint {pk_cols} "
                    f"on {destination_table_full_path}."
                )


# ---------------------------------------------------------------------------
# Individual cleaning/casting helpers (one per step)
# ---------------------------------------------------------------------------


def _filter_columns(
    df: DataFrame,
    columns: list[str],
    expected_types: list[str],
    context: str = "",
    skip_warning: bool = False,
) -> list[str]:
    """Filter *columns* to those present in *df* with a dtype in *expected_types*."""
    df_schema = dict(df.dtypes)
    prefix = f"[{context}] " if context else ""
    valid: list[str] = []
    for col in columns:
        if col not in df_schema and not skip_warning:
            log.warning(f"{prefix}Column '{col}' not found in DataFrame.")
        elif df_schema[col] not in expected_types and not skip_warning:
            log.warning(
                f"{prefix}: Column '{col}' skipped, dtype '{df_schema[col]}' "
                f"not in expected types {expected_types}."
            )
        else:
            valid.append(col)
    return valid


def cast_columns_to_bool(df: DataFrame, columns: list[str]) -> DataFrame:
    """Cast columns currently typed as ``bool`` to explicit BooleanType."""
    cols = _filter_columns(df, columns, TYPES_BOOL, context="cast_columns_to_bool")
    if not cols:
        return df
    log.debug(f"Columns to cast to bool: {cols}")
    return df.withColumns({col: f.col(col).cast("boolean") for col in cols})


def cast_columns_to_date(
    df: DataFrame,
    columns: list[str],
    date_format: str | None = None,
    formats: list[str] | None = None,
) -> DataFrame:
    """Parse string columns representing dates into DateType.

    Format resolution priority:
    1. ``date_format`` — single explicit format; only this is tried (no coalesce)
    2. ``formats``     — custom list of formats to try in order (coalesce)
    3. ``DEFAULT_DATE_FORMATS`` — module default, used when neither is set
    """
    cols = _filter_columns(df, columns, TYPES_STR, context="cast_columns_to_date")
    if not cols:
        return df

    if date_format is not None:
        effective_formats = [date_format]
    elif formats is not None:
        effective_formats = formats
    else:
        effective_formats = DEFAULT_DATE_FORMATS

    log.debug(f"Columns to cast to date: {cols} (formats: {effective_formats})")

    def _parse(col_name: str):
        if not effective_formats:
            return f.try_to_date(f.col(col_name))
        if len(effective_formats) == 1:
            return f.try_to_date(f.col(col_name), f.lit(effective_formats[0]))
        return f.coalesce(*[f.try_to_date(f.col(col_name), f.lit(fmt)) for fmt in effective_formats])

    return df.withColumns({col: _parse(col) for col in cols})


def clean_string_columns(
    df: DataFrame, columns: list[str], is_clean_all: bool, is_nullify_empty: bool = False
) -> DataFrame:
    """Fix encoding mojibake, normalize Unicode (NFC), trim whitespace, and nullify empty strings 
    (if requested).

    Mojibake repair: detects strings where UTF-8 bytes were misinterpreted as Latin-1/ISO-8859-1
    (e.g. 'Ã©' instead of 'é') and re-encodes them correctly.

    NFC normalization: recomposes any remaining decomposed accent sequences (NFD) —
    e.g. 'e' + U+0301 (combining acute) becomes 'é'.
    """
    cols = _filter_columns(
        df, columns, TYPES_STR, context="clean_string_columns", skip_warning=is_clean_all
    )
    if not cols:
        return df
    log.debug(f"String columns to clean: {cols}")

    # Step 1: Fix mojibake — re-interpret Latin-1 chars as UTF-8 bytes when pattern detected
    df = df.withColumns(
        {
            col: f.when(
                f.col(col).rlike(_MOJIBAKE_RE),
                f.decode(f.encode(f.col(col), "ISO-8859-1"), "UTF-8"),
            ).otherwise(f.col(col))
            for col in cols
        }
    )

    # Step 2: trim + nullify empty (if requested)
    return df.withColumns(
        {
            col: f.nullif(f.trim(f.col(col)), f.lit("")) if is_nullify_empty else f.trim(f.col(col))
            for col in cols
        }
    )

def clean_id_columns(
    df: DataFrame, columns: list[str], min_length: int, max_length: int
) -> DataFrame:
    """Validate numeric ID format — keep only digits within length bounds, null the rest."""
    cols = _filter_columns(df, columns, TYPES_STR, context="clean_id_columns")
    pattern = f"^[0-9]{{{min_length},{max_length}}}$"
    if not cols:
        return df
    log.debug(f"ID columns to clean: {cols}")
    return df.withColumns(
        {
            col: f.when(
                f.trim(f.col(col)).rlike(pattern), f.trim(f.col(col))
            ).otherwise(f.lit(None))
            for col in cols
        }
    )


def propagate_pii_tags_with_lineage(
    spark: SparkSession,
    catalog_source: str,
    catalog_target: str,
    schema: str,
    tables_names: list[str],
    simulate: bool = False,
) -> None:
    """Propagate PII column tags from source to target using column lineage."""
    tables_filter = ", ".join(f"'{t}'" for t in tables_names)
    lineage_rows = spark.sql(f"""
        SELECT DISTINCT
            cl.target_table_full_name,
            cl.target_column_name,
            cl.source_table_full_name,
            cl.source_column_name,
            ct.tag_name,
            ct.tag_value
        FROM system.access.column_lineage cl
        INNER JOIN system.information_schema.column_tags ct
            ON concat(ct.catalog_name, '.', ct.schema_name, '.', ct.table_name)
            = cl.source_table_full_name
        AND ct.column_name = cl.source_column_name
        WHERE cl.source_table_full_name  LIKE '{catalog_source}.{schema}.%'
        AND cl.target_table_full_name  LIKE '{catalog_target}.{schema}.%'
        AND ct.table_name              IN ({tables_filter})
        AND ct.tag_name                LIKE '{PII_TAG_PREFIX}%'
    """).collect()

    n_applied = 0
    for row in lineage_rows:
        if not simulate:
            spark.sql(f"""
                ALTER TABLE {row.target_table_full_name}
                ALTER COLUMN {row.target_column_name}
                SET TAGS ('{row.tag_name}' = '{row.tag_value or ""}')
            """)
            n_applied += 1
        log.debug(
            f"From source {row.source_table_full_name}.{row.source_column_name} "
            f"tagged {row.target_table_full_name}.{row.target_column_name} "
            f"← {row.tag_name} (value='{row.tag_value}')"
        )

    log.info(
        f"Propagated {n_applied} column tag(s) "
        f"from {catalog_source}.{schema} → {catalog_target}.{schema}."
    )
