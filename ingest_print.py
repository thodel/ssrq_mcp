#!/usr/bin/env python3
"""
ingest_print.py — load the PRINTED SSRQ volumes (re-OCR, thodel/ssrq_print) into the documents table.

Source: ssrq_print's `python -m ssrq_print.export_mcp`, which writes
    <export>/documents.jsonl   one row per source piece, shaped like this table's rows
    <export>/register.jsonl    the merged registers of all printed volumes (super-register)
    <export>/manifest.json     volumes, counts, and whether ALL volumes were finished

    python ingest_print.py --export /path/to/out/mcp --db /data/ssrq.db
    GPUSTACK_API_KEY=... python embed_db.py --db /data/ssrq.db      # then embed the new rows (resumes)

The printed volumes share `documents` with the editions, told apart by `source = 'print'` (ids SSRQ-PRINT-…).
`extra` holds what only they carry: regest, transmission (description), the page in SSRQ Online's viewer,
and the super-register ids that cite the piece. Re-running replaces the print rows and leaves the editions alone
(and ingest_editio.py now leaves the print rows alone).

The volumes are © Rechtsquellenstiftung; the text is a machine transcription — answers quoting a print row
must say so and link `url` (the website) and `extra.viewer` (the printed page in SSRQ Online).

By design this refuses a partial export (manifest.complete = false): the printed volumes go in once, all
together, after the OCR of the last volume. --allow-partial is for a test database only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import db as db_module

COLS = ("id", "canton", "volume", "title", "lang", "origin_from", "origin_to", "place", "text", "n_chars", "url",
        "source", "extra")

REGISTER_SQL = """
CREATE TABLE IF NOT EXISTS print_register (
  id TEXT PRIMARY KEY,      -- super-register id, e.g. person000123
  type TEXT,                -- person | family | place | org | subject | glossary
  label TEXT, variants TEXT, descriptions TEXT, years TEXT, volumes TEXT,
  documents TEXT,           -- JSON list of SSRQ-PRINT-… ids
  links TEXT,               -- JSON: per source (hls, ssrq, kf) {status, best, candidates}
  ssrq_id TEXT,             -- the SSRQ person/org id when the link is a clear match
  url TEXT
);
CREATE INDEX IF NOT EXISTS idx_print_register_ssrq ON print_register(ssrq_id);
CREATE INDEX IF NOT EXISTS idx_print_register_label ON print_register(label);
"""


def load(export: Path, db_path: str, allow_partial: bool = False, quiet: bool = False) -> tuple[int, int]:
    manifest = json.loads((export / "manifest.json").read_text())
    if not manifest.get("complete") and not allow_partial:
        raise SystemExit(f"export is partial ({len(manifest.get('unfinished', []))} volumes unfinished); "
                         "the printed volumes are ingested once, when all are done (--allow-partial for tests)")
    conn = sqlite3.connect(db_path)
    conn.executescript(db_module.SCHEMA_SQL)
    db_module.ensure_document_source(conn)
    conn.executescript(REGISTER_SQL)
    conn.execute("DELETE FROM documents WHERE source = 'print'")
    conn.execute("DELETE FROM print_register")
    placeholders = ",".join("?" * len(COLS))
    n_docs = 0
    batch = []
    with (export / "documents.jsonl").open() as fh:
        for line in fh:
            r = json.loads(line)
            r["source"] = "print"
            r["extra"] = json.dumps(r.get("extra") or {}, ensure_ascii=False)
            batch.append(tuple(r.get(c) for c in COLS))
            if len(batch) >= 500:
                conn.executemany(f"INSERT OR REPLACE INTO documents ({','.join(COLS)}) VALUES ({placeholders})", batch)
                n_docs += len(batch)
                batch.clear()
    if batch:
        conn.executemany(f"INSERT OR REPLACE INTO documents ({','.join(COLS)}) VALUES ({placeholders})", batch)
        n_docs += len(batch)
    # external-content FTS: rebuilt from the table, as ingest_editio.py does
    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
    n_reg = 0
    with (export / "register.jsonl").open() as fh:
        for line in fh:
            c = json.loads(line)
            ssrq = (c.get("links") or {}).get("ssrq") or {}
            ssrq_id = ssrq["best"]["id"] if ssrq.get("status") == "match" else None
            conn.execute("INSERT OR REPLACE INTO print_register VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                c["id"], c["type"], c["label"], json.dumps(c.get("variants", []), ensure_ascii=False),
                json.dumps(c.get("descriptions", []), ensure_ascii=False), json.dumps(c.get("years", [])),
                json.dumps(c.get("volumes", [])), json.dumps(c.get("documents", [])),
                json.dumps(c.get("links", {}), ensure_ascii=False), ssrq_id, c.get("url")))
            n_reg += 1
    conn.commit()
    if not quiet:
        by_source = dict(conn.execute("SELECT source, COUNT(*) FROM documents GROUP BY source").fetchall())
        print(f"print volumes: {n_docs} documents, {n_reg} register entries; documents by source: {by_source}",
              file=sys.stderr)
    conn.close()
    return n_docs, n_reg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--export", required=True, type=Path, help="ssrq_print out/mcp directory")
    ap.add_argument("--db", required=True)
    ap.add_argument("--allow-partial", action="store_true")
    a = ap.parse_args(argv)
    load(a.export, a.db, a.allow_partial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
