"""Copy every conversation and history row of the UAT Lakebase into the PROD Lakebase. Run from the repo root:

    python runbooks/deploiement_prod/copie_lakebase_uat_vers_prod.py           # plan only, nothing written
    python runbooks/deploiement_prod/copie_lakebase_uat_vers_prod.py --apply   # copies, in one transaction

CLI profiles UAT (read only) and PROD (write). The PROD tables must exist: the PROD app creates them at its first start.
UAT is never modified. PROD rows are never modified or deleted: UAT rows are added after them.

- Integer ids referenced by another table (compare_messages, chat_messages, chat_turns, impact_requests) are shifted
  by the current max id of the PROD table, and every column pointing to them is shifted the same way, so no PROD row
  is overwritten and every feedback still points to its message.
- Integer ids that nothing references (users, llm_requests, app_errors, caches...) are left to PROD's sequence.
- Text ids (chat_sessions) are kept; a session id already in PROD stops the copy (it was already copied).
- /Volumes/uat_proj/ becomes /Volumes/prod_proj/ in every value (comparison files, see the README for the files).
- Not copied: tables the PROD parsing rebuilds (doc_catalog, doc_references, catalog_metadata) and the legacy
  tables UAT already copied into the current ones at its own start (messages, feedbacks, errors, knowledge_base_metadata).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utils"))
from ops_config import lakebase_connect

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("copie_lakebase_uat_vers_prod")

EXCLUDED = {"doc_catalog", "doc_references", "catalog_metadata", "messages", "feedbacks", "errors", "knowledge_base_metadata"}
PATH_FROM, PATH_TO = "/Volumes/uat_proj/", "/Volumes/prod_proj/"
BATCH = 500


def tables_of(cur) -> set[str]:
    """Base tables of the public schema.

    :param cur: cursor of the database
    :return: table names
    """
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
    return {r[0] for r in cur.fetchall()}


def columns_of(cur, table: str) -> dict[str, str]:
    """Columns of a table with their full SQL type, in table order.

    :param cur: cursor of the database
    :param table: table name
    :return: column name -> SQL type
    """
    cur.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
        (f"public.{table}",),
    )
    return dict(cur.fetchall())


def foreign_keys(cur) -> list[tuple[str, str, str, str]]:
    """Single-column foreign keys of the public schema.

    :param cur: cursor of the database
    :return: (table, column, referenced table, referenced column)
    """
    cur.execute(
        "SELECT c.conrelid::regclass::text, a.attname, c.confrelid::regclass::text, af.attname "
        "FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
        "JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = c.confkey[1] "
        "WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace"
    )
    return [tuple(x.replace("public.", "") for x in r) for r in cur.fetchall()]


def ordered(tables: set[str], fks) -> list[str]:
    """Tables ordered so that a referenced table comes before the tables pointing to it.

    :param tables: tables to order
    :param fks: foreign keys, as returned by foreign_keys
    :return: ordered table names
    """
    deps = {t: {ref for (tab, _, ref, _) in fks if tab == t and ref in tables and ref != t} for t in tables}
    out: list[str] = []
    while deps:
        ready = sorted(t for t, d in deps.items() if not d - set(out))
        if not ready:
            raise RuntimeError(f"circular foreign keys between {sorted(deps)}")
        out += ready
        for t in ready:
            del deps[t]
    return out


def main() -> None:
    """Plan the copy (row counts, id shifts), then copy when ``--apply`` is given.

    :return: None
    """
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="copy (default: plan only)")
    args = ap.parse_args()

    src = lakebase_connect("UAT")
    src.set_session(readonly=True)
    dst = lakebase_connect("PROD")
    try:
        s, d = src.cursor(), dst.cursor()
        missing = sorted(tables_of(s) - tables_of(d) - EXCLUDED)
        if missing:
            raise SystemExit(f"Missing in PROD: {missing}. Start the PROD app once (deploy-prod) so it creates them, then rerun.")
        fks = [fk for fk in foreign_keys(d) if fk[0] not in EXCLUDED and fk[2] not in EXCLUDED]
        tables = ordered(tables_of(s) - EXCLUDED, fks)
        referenced = {(ref, refcol) for (_, _, ref, refcol) in fks}

        s.execute("SELECT to_regclass('public.chat_sessions') IS NOT NULL")
        if s.fetchone()[0]:
            s.execute("SELECT id FROM chat_sessions")
            uat_sessions = [r[0] for r in s.fetchall()]
            if uat_sessions:
                d.execute("SELECT count(*) FROM chat_sessions WHERE id = ANY(%s)", (uat_sessions,))
                if d.fetchone()[0]:
                    raise SystemExit("UAT chat sessions are already in PROD: the copy was already made. Nothing done.")

        # Shift of each referenced integer id: the PROD max id, so UAT rows land after the PROD ones.
        shift: dict[str, int] = {}
        for table in tables:
            cols = columns_of(d, table)
            if (table, "id") in referenced and cols.get("id", "").startswith(("integer", "bigint")):
                d.execute(f'SELECT COALESCE(max(id), 0) FROM "{table}"')
                shift[table] = d.fetchone()[0]

        plan = []
        for table in tables:
            src_cols, dst_cols = columns_of(s, table), columns_of(d, table)
            cols = [c for c in src_cols if c in dst_cols]
            keep_id = "id" in cols and ((table, "id") in referenced or not dst_cols["id"].startswith(("integer", "bigint")))
            if "id" in cols and not keep_id:
                cols.remove("id")
            shifted = {"id": shift[table]} if table in shift and keep_id else {}
            for tab, col, ref, refcol in fks:
                if tab == table and refcol == "id" and ref in shift and col in cols:
                    shifted[col] = shift[ref]
            s.execute(f'SELECT count(*) FROM "{table}"')
            n_src = s.fetchone()[0]
            d.execute(f'SELECT count(*) FROM "{table}"')
            n_dst = d.fetchone()[0]
            logger.info(
                "%-26s UAT %7d rows -> PROD (%d rows today)%s%s", table, n_src, n_dst,
                f" | shifted {shifted}" if shifted else "", "" if keep_id or "id" not in src_cols else " | new ids",
            )
            plan.append((table, cols, dst_cols, shifted, n_src))

        if not args.apply:
            logger.info("Plan only, nothing written. Rerun with --apply to copy.")
            return

        for table, cols, dst_cols, shifted, n_src in plan:
            if not n_src:
                continue
            select = ", ".join(f'"{c}"::text' for c in cols)
            insert = (
                f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES '
            )
            placeholder = "(" + ", ".join(f"%s::{dst_cols[c]}" for c in cols) + ")"
            read = src.cursor(name=f"read_{table}")
            read.itersize = BATCH
            read.execute(f'SELECT {select} FROM "{table}"')
            copied = 0
            while True:
                rows = read.fetchmany(BATCH)
                if not rows:
                    break
                values = []
                for row in rows:
                    out = []
                    for c, v in zip(cols, row):
                        if v is not None and c in shifted:
                            v = str(int(v) + shifted[c])
                        elif v is not None and PATH_FROM in v:
                            v = v.replace(PATH_FROM, PATH_TO)
                        out.append(v)
                    values.append(d.mogrify(placeholder, out).decode())
                d.execute(insert + ", ".join(values) + " ON CONFLICT DO NOTHING")
                copied += d.rowcount
            read.close()
            logger.info("%-26s %d / %d rows copied", table, copied, n_src)
            if "id" in cols and dst_cols["id"].startswith(("integer", "bigint")):
                d.execute(f"SELECT setval(pg_get_serial_sequence('\"{table}\"', 'id'), GREATEST((SELECT max(id) FROM \"{table}\"), 1))")
        dst.commit()
        logger.info("Done: every table copied in one transaction.")
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()
