#!/usr/bin/env python3
"""
migrate_parent_id.py — rename this repo's own chunk schema to the shared one (#18).

Until #18 the index named the parent column `chunks.doc_id` and the run count
`embedding_runs.n_articles`; the shared vectorstore.SCHEMA_SQL says `parent_id`
and `n_parents`. Nothing else differs, and ch-h-bot#555 measured that every one
of the 18,407 passages stays row-identical under the shared embed_db.py, so
the vectors stay: this is a rename, not a re-embed.

    python migrate_parent_id.py --db /home/tobias/mcp/data/ssrq.db

One transaction, then PRAGMA integrity_check. Idempotent: a database that is
already on parent_id is reported and left alone. **Make the backup first**
(`cp ssrq.db ssrq.db.pre-parent-id`); this script does not. Stop the server
before running it — the old db.py asks for doc_id, and a running old server
on a renamed database answers every search_semantic with «no such column».

The DROP INDEX is deliberate: RENAME COLUMN re-points the old index but keeps
its name, and vectorstore.SCHEMA_SQL would then create ix_chunks_parent next
to it, two identical indexes. SQLite cannot rename an index.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import corpus

OLD_COLUMN, NEW_COLUMN = "doc_id", "parent_id"
OLD_INDEX, NEW_INDEX = "ix_chunks_entry", "ix_chunks_parent"
OLD_RUNS_COLUMN, NEW_RUNS_COLUMN = "n_articles", "n_parents"
MIN_SQLITE = (3, 25, 0)          # ALTER TABLE … RENAME COLUMN

MIGRATION_SQL = f"""
ALTER TABLE chunks RENAME COLUMN {OLD_COLUMN} TO {NEW_COLUMN};
ALTER TABLE embedding_runs RENAME COLUMN {OLD_RUNS_COLUMN} TO {NEW_RUNS_COLUMN};
DROP INDEX {OLD_INDEX};
CREATE INDEX {NEW_INDEX} ON chunks({NEW_COLUMN});
"""


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def indexes(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA index_list({table})")]


def schema_state(conn: sqlite3.Connection) -> str:
    """'old' (doc_id), 'new' (parent_id), or a sentence saying what is odd."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("chunks", "embedding_runs"):
        if table not in tables:
            return f"no table {table}: this database has no semantic index to migrate"
    chunk_cols, run_cols = columns(conn, "chunks"), columns(conn, "embedding_runs")
    if (OLD_COLUMN in chunk_cols and OLD_RUNS_COLUMN in run_cols
            and NEW_COLUMN not in chunk_cols):
        return "old"
    if (NEW_COLUMN in chunk_cols and NEW_RUNS_COLUMN in run_cols
            and OLD_COLUMN not in chunk_cols and OLD_INDEX not in indexes(conn, "chunks")):
        return "new"
    return (f"neither the old nor the shared schema: chunks{chunk_cols}, "
            f"embedding_runs{run_cols}, indexes {indexes(conn, 'chunks')}")


def counts(conn: sqlite3.Connection, parent_col: str) -> dict:
    return {
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "parents": conn.execute(
            f"SELECT COUNT(DISTINCT {parent_col}) FROM chunks").fetchone()[0],
        "vectors": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "pending": conn.execute(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e "
            "ON e.chunk_id = c.chunk_id WHERE e.chunk_id IS NULL").fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM embedding_runs").fetchone()[0],
    }


def migrate(conn: sqlite3.Connection) -> str:
    """Run the rename in one transaction; return the integrity_check verdict."""
    conn.isolation_level = None          # we drive the transaction ourselves
    conn.execute("BEGIN")
    try:
        for statement in MIGRATION_SQL.strip().split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get(corpus.DB_ENV, corpus.DEFAULT_DB))
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"ERROR: database not found at {args.db}", file=sys.stderr)
        return 1
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        print(f"ERROR: sqlite {sqlite3.sqlite_version} cannot RENAME COLUMN "
              f"(needs {'.'.join(map(str, MIN_SQLITE))})", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        state = schema_state(conn)
        if state == "new":
            print(f"{args.db}: already on {NEW_COLUMN}, nothing to do: "
                  f"{counts(conn, NEW_COLUMN)}")
            return 0
        if state != "old":
            print(f"ERROR: {args.db}: {state}", file=sys.stderr)
            return 1

        before = counts(conn, OLD_COLUMN)
        print(f"before: {before}")
        verdict = migrate(conn)
        after = counts(conn, NEW_COLUMN)
        print(f"after:  {after}")
        print(f"integrity_check: {verdict}")
        if verdict != "ok":
            print("ERROR: integrity_check failed — restore the backup", file=sys.stderr)
            return 2
        if before != after:
            print("ERROR: the counts changed — restore the backup", file=sys.stderr)
            return 2
        print(f"{args.db}: chunks.{OLD_COLUMN} → {NEW_COLUMN}, "
              f"embedding_runs.{OLD_RUNS_COLUMN} → {NEW_RUNS_COLUMN}, "
              f"{OLD_INDEX} → {NEW_INDEX}. "
              f"Next: git pull, start the server, embed_db.py --status (pending must be 0).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
