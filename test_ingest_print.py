"""ingest_print.py: printed volumes share `documents` with the editions without either rebuild deleting the other."""
import json
import sqlite3

import pytest

import db as db_module
import ingest_print


def _export(tmp_path, complete=True):
    d = tmp_path / "mcp"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"complete": complete, "unfinished": [] if complete else ["X"]}))
    row = {"id": "SSRQ-PRINT-AG_I_1-102", "canton": "AG", "volume": "AG_I_1", "title": "Pfändung um gichtige Schuld.",
           "lang": "de", "origin_from": 1533, "origin_to": None, "place": "", "text": "Wölcher eim ſchuldig der mag in pfenden",
           "n_chars": 38, "url": "https://example.org/d/102.html", "extra": {"regest": "", "viewer": "x#p_197"}}
    (d / "documents.jsonl").write_text(json.dumps(row) + "\n")
    reg = {"id": "person000001", "type": "person", "label": "Erlach, von, Ulrich", "variants": [], "descriptions": [],
           "years": [], "volumes": ["BE_II_1.1"], "documents": [],
           "links": {"ssrq": {"status": "match", "best": {"id": "per027242"}}}, "url": "u"}
    (d / "register.jsonl").write_text(json.dumps(reg) + "\n")
    return d


def test_ingest_keeps_editions_and_refuses_partial(tmp_path):
    dbp = str(tmp_path / "t.db")
    conn = sqlite3.connect(dbp)
    conn.executescript(db_module.SCHEMA_SQL)
    conn.execute("INSERT INTO documents (id, canton, volume, title, text, n_chars) VALUES ('SSRQ-ZH-1', 'ZH', 'V', 't', 'x', 1)")
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit):
        ingest_print.load(_export(tmp_path / "a", complete=False) if (tmp_path / "a").mkdir() is None else None, dbp)
    n_docs, n_reg = ingest_print.load(_export(tmp_path), dbp, quiet=True)
    assert (n_docs, n_reg) == (1, 1)
    conn = sqlite3.connect(dbp)
    assert dict(conn.execute("SELECT source, COUNT(*) FROM documents GROUP BY source")) == {"editio": 1, "print": 1}
    assert conn.execute("SELECT ssrq_id FROM print_register").fetchone()[0] == "per027242"
    assert conn.execute("SELECT COUNT(*) FROM documents_fts WHERE documents_fts MATCH 'pfenden'").fetchone()[0] == 1
