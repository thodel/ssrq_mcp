"""db.py — read-only SQLite helpers for the SSRQ MCP server."""
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Optional

_DB_PATH = "ssrq.db"

MAX_LIMIT = 500          # ceiling for any caller-supplied limit
VARIANT_LIMIT = 500      # name variants attached to a single record
# Rows in the ssrq://orgs resource. Kept well under the ~150k-character result
# limit Claude.ai and Claude Desktop apply to a tool or resource payload: at 9999
# rows this resource ran to roughly a megabyte and was silently unusable there.
ORG_INDEX_LIMIT = 1000

# The schema this server expects. The real database is produced by the SSRQ ETL
# from the RDF-TTL dump; this constant is the contract that server and ETL share,
# and the tests build their fixtures from it.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS persons (
  id TEXT PRIMARY KEY, uri TEXT, etype TEXT, label TEXT, label_lang TEXT,
  std_name TEXT, forename TEXT, surname TEXT, sex TEXT,
  first_year INTEGER, last_year INTEGER, years TEXT,
  org_ids TEXT, spouse_ids TEXT, mother_ids TEXT, father_ids TEXT, loc_ids TEXT,
  orig_names TEXT, std_names TEXT
);
CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY, uri TEXT, etype TEXT, label TEXT, std_name TEXT,
  surname TEXT, alias_of TEXT, org_type TEXT
);
CREATE TABLE IF NOT EXISTS name_index (
  name_text TEXT, ssrq_id TEXT, is_orig INTEGER
);
CREATE INDEX IF NOT EXISTS idx_name_index_text ON name_index(name_text);
CREATE INDEX IF NOT EXISTS idx_name_index_id   ON name_index(ssrq_id);

-- Edited law sources from SSRQ-SDS-FDS/editio-data (CC BY-NC-SA 4.0).
--
-- Until now this server held an authority file and nothing else: it could say
-- who a name referred to, never what a document said. These are the documents —
-- transcribed charters, statutes and ordinances, 1050 to 1846 — which makes
-- SSRQ a source of evidence rather than only of entity context.
--
-- origin_from/origin_to come from <origDate>, which is the date the document
-- was issued. The TEI also carries <date type="electronic">, the date the
-- edition was published; conflating the two would date a fifteenth-century
-- charter to 2022.
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,          -- the TEI <idno>, e.g. SSRQ-ZH-NF_I_1_3-1-1
  canton TEXT,                  -- FR, NE, SG, VD, ZH
  volume TEXT,                  -- the edition volume directory
  title TEXT,
  lang TEXT,
  origin_from INTEGER,          -- year the document was issued
  origin_to INTEGER,            -- end of the range, where dated as a span
  place TEXT,
  text TEXT,                    -- the transcription, tags stripped
  n_chars INTEGER,
  url TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_year   ON documents(origin_from);
CREATE INDEX IF NOT EXISTS idx_documents_canton ON documents(canton);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  title, text, content='documents', content_rowid='rowid'
);
"""


# Chunk and vector tables, mirroring kf_mcp. Kept separate from SCHEMA_SQL so
# a database built before the editions were ingested can be upgraded in place
# rather than rebuilt.
EMBEDDING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT    PRIMARY KEY,   -- "<doc_id>#<chunk_index>"
    doc_id    TEXT    NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    text        TEXT    NOT NULL,      -- the transcription as edited, tags stripped
    UNIQUE (doc_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT    PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    model    TEXT    NOT NULL,
    dims     INTEGER NOT NULL,
    vector   BLOB    NOT NULL          -- float32, little-endian, L2-normalised
);
CREATE TABLE IF NOT EXISTS embedding_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    model       TEXT NOT NULL,
    dims        INTEGER,
    base_url    TEXT,
    chunk_chars INTEGER,
    chunk_overlap INTEGER,
    n_articles  INTEGER,
    n_chunks    INTEGER,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_entry ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS ix_embeddings_model ON embeddings(model);
"""


# ── Editions: full text and meaning ──────────────────────────────────────────

_DOC_FTS_SQL = (
    "SELECT d.id, d.title, d.canton, d.volume, d.origin_from, d.origin_to, "
    "d.lang, d.place, d.url, "
    "snippet(documents_fts, 1, '<mark>', '</mark>', '…', 32) AS snippet "
    "FROM documents_fts JOIN documents d ON documents_fts.rowid = d.rowid "
    "WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?"
)


def quote_fts(query):
    """Rewrite a query as quoted FTS5 phrases, one per word (implicit AND).
    Strips the characters FTS5 treats as syntax so no input can be a syntax error."""
    tokens = [t for t in re.split(r'\s+', re.sub(r'["\*\(\):^-]', ' ', query)) if t]
    return ' '.join(f'"{t}"' for t in tokens)

def search_documents(query, limit=20):
    """Keyword search over the transcriptions.

    Useful when the caller already knows the spelling — a signature, a place
    name, a formula. For a question in modern German, search_semantic reaches
    this material and this does not: the orthography is the scribe's.
    """
    limit = clamp(limit, 20)
    if not query or not query.strip():
        return [{"error": "Empty query."}]
    with conn() as c:
        for q in (query, quote_fts(query)):
            if not q:
                break
            try:
                return r(c.execute(_DOC_FTS_SQL, (q, limit)).fetchall())
            except sqlite3.OperationalError:
                continue
    return [{"error": f"Could not parse query: {query!r}"}]


def get_document(doc_id, with_text=True):
    """One edited document, by its TEI idno."""
    cols = ("id, canton, volume, title, lang, origin_from, origin_to, place, "
            "n_chars, url" + (", text" if with_text else ""))
    with conn() as c:
        row = c.execute(f"SELECT {cols} FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def _load_matrix(model):
    """(chunk_ids, matrix) for a model, loaded once and cached."""
    key = (_DB_PATH, model)
    cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for semantic search — pip install numpy") from exc
    with conn() as c:
        rows = c.execute(
            "SELECT chunk_id, dims, vector FROM embeddings WHERE model = ? "
            "ORDER BY chunk_id", (model,)).fetchall()
    if not rows:
        raise RuntimeError(
            f"no embeddings for model {model!r} in {_DB_PATH}. "
            "Run embed_db.py to build the semantic index.")
    dims = rows[0]["dims"]
    chunk_ids = [row["chunk_id"] for row in rows]
    # One contiguous buffer: the difference between a matrix multiply and
    # thousands of small ones.
    buffer = b"".join(row["vector"] for row in rows)
    matrix = np.frombuffer(buffer, dtype="<f4").reshape(len(rows), dims)
    _VECTOR_CACHE[key] = (chunk_ids, matrix)
    return chunk_ids, matrix


def search_semantic(query_vector, limit=20, model=None, year_from=None,
                    year_to=None, per_document=2):
    """Passages closest in meaning to an already-embedded query.

    ``per_document`` caps how many passages one document may contribute, so a long
    document cannot fill the result set and crowd out the other documents that
    answer the question. ``year_from``/``year_to`` restrict to a period, which
    for this corpus is often the point of the question.
    """
    import numpy as np

    limit = clamp(limit, 20)
    model = model or os.environ.get("SSRQ_EMBED_MODEL", "qwen3-embedding-0.6b")
    chunk_ids, matrix = _load_matrix(model)

    query = np.asarray(query_vector, dtype="float32")
    if query.shape[0] != matrix.shape[1]:
        raise ValueError(
            f"query has {query.shape[0]} dimensions, index has {matrix.shape[1]}")
    norm = float(np.linalg.norm(query)) or 1.0
    scores = matrix @ (query / norm)

    # Take a generous slice before filtering: the year filter and the per-entry
    # cap both discard candidates.
    fetch = min(len(chunk_ids), max(limit * 8, limit + 50))
    candidates = np.argpartition(-scores, fetch - 1)[:fetch]
    candidates = candidates[np.argsort(-scores[candidates])]
    picked = [(chunk_ids[i], float(scores[i])) for i in candidates]

    by_id = {}
    with conn() as c:
        for start in range(0, len(picked), 400):
            window = picked[start:start + 400]
            sql = _SEMANTIC_SQL.format(placeholders=",".join("?" * len(window)))
            for row in c.execute(sql, [cid for cid, _ in window]).fetchall():
                by_id[row["chunk_id"]] = row

    out, seen = [], {}
    for chunk_id, score in picked:
        row = by_id.get(chunk_id)
        if row is None:
            continue                      # vector outlived its chunk
        year = row["year"]
        if year_from is not None and (year is None or year < year_from):
            continue
        if year_to is not None and (year is None or year > year_to):
            continue
        if seen.get(row["doc_id"], 0) >= per_document:
            continue
        seen[row["doc_id"]] = seen.get(row["doc_id"], 0) + 1
        out.append({
            "id": row["doc_id"],
            "chunk_id": chunk_id,
            "title": row["title"],
            "short_id": row["short_id"],
            "year": year,
            "source": row["source"],
            "snippet": row["text"],
            "score": round(score, 4),
            "chunk_index": row["chunk_index"],
            "char_start": row["char_start"],
            "char_end": row["char_end"],
        })
        if len(out) >= limit:
            break
    return out

def semantic_stats(model=None):
    """Coverage of the semantic index, and the runs that produced it."""
    with conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='embeddings'").fetchone():
            return {"indexed": False,
                    "reason": "no embeddings table; run embed_db.py"}
        by_model = c.execute(
            "SELECT model, COUNT(*) n, MAX(dims) d FROM embeddings GROUP BY model"
        ).fetchall()
        n_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_total = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        n_indexed = c.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM chunks").fetchone()[0]
        runs = c.execute(
            "SELECT run_id, started_at, finished_at, model, dims, n_chunks, notes "
            "FROM embedding_runs ORDER BY started_at DESC LIMIT 5").fetchall()
    return {
        "indexed": bool(by_model),
        "n_chunks": n_chunks,
        "n_entries_indexed": n_indexed,
        "n_entries_total": n_total,
        "coverage": round(n_indexed / n_total, 4) if n_total else 0.0,
        "models": [{"model": m["model"], "n_vectors": m["n"], "dims": m["d"]}
                   for m in by_model],
        "recent_runs": r(runs),
    }


_SEMANTIC_SQL = (
    "SELECT c.chunk_id,c.doc_id,c.chunk_index,c.char_start,c.char_end,c.text,"
    "e.title,e.short_id,e.origin_from,e.source "
    "FROM chunks c JOIN documents e ON e.id=c.doc_id "
    "WHERE c.chunk_id IN ({placeholders})"
)


def warm_semantic_index(model):
    """Load the vectors now and report what was loaded, or why it could not be."""
    try:
        chunk_ids, matrix = _load_matrix(model)
    except RuntimeError as exc:
        return {"ready": False, "model": model, "reason": str(exc)}
    return {"ready": True, "model": model, "n_chunks": len(chunk_ids),
            "dims": int(matrix.shape[1]),
            "megabytes": round(matrix.nbytes / 1_048_576, 1)}



def set_db_path(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path


@contextmanager
def conn():
    # mode=ro fails loudly on a missing file instead of silently creating an empty
    # database; query_only additionally blocks writes through this connection.
    con = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    try:
        yield con
    finally:
        # sqlite3's own context manager only ends the transaction — it never closes
        # the connection, so the close has to happen here.
        con.close()


def r(rows) -> list[dict]:
    return [dict(row) for row in rows]


def clamp(limit, default, cap=MAX_LIMIT) -> int:
    """Constrain a caller-supplied limit. SQLite reads LIMIT -1 as unbounded, so an
    unchecked negative value would return the whole table; anything invalid or out
    of range falls back to the tool's own default."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return min(n, cap) if n >= 1 else default


def clamp_offset(offset) -> int:
    try:
        return max(int(offset), 0)
    except (TypeError, ValueError):
        return 0


def like_pattern(query: Optional[str]) -> str:
    """Substring pattern for LIKE, with the wildcards escaped so a query of '%' or
    '_' matches those characters literally instead of the whole table. Pairs with
    ESCAPE '\\' in the SQL."""
    escaped = (query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _split_ids(ids_str: Optional[str]) -> list[str]:
    """The relation columns hold comma-joined ids ('per000001,per000002')."""
    return [i.strip() for i in (ids_str or "").split(",") if i.strip()]


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone() is not None


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats() -> dict:
    with conn() as c:
        return {
            "n_persons":    c.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
            "n_orgs":       c.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
            "n_name_index": c.execute("SELECT COUNT(*) FROM name_index").fetchone()[0],
            "year_min":     c.execute("SELECT MIN(first_year) FROM persons WHERE first_year IS NOT NULL").fetchone()[0],
            "year_max":     c.execute("SELECT MAX(last_year)  FROM persons WHERE last_year  IS NOT NULL").fetchone()[0],
        }


# ── Persons ───────────────────────────────────────────────────────────────────

PERSON_BRIEF = ("id, label, std_name, forename, surname, sex, "
                "first_year, last_year, years, orig_names")


def list_persons(limit=50, offset=0) -> list[dict]:
    with conn() as c:
        return r(c.execute(
            f"SELECT {PERSON_BRIEF} FROM persons ORDER BY id LIMIT ? OFFSET ?",
            (clamp(limit, 50), clamp_offset(offset))
        ).fetchall())


def search_persons(query: str, limit=50) -> list[dict]:
    with conn() as c:
        return r(c.execute(
            f"SELECT {PERSON_BRIEF} FROM persons "
            "WHERE std_name LIKE ?1 ESCAPE '\\' OR label LIKE ?1 ESCAPE '\\' "
            "OR orig_names LIKE ?1 ESCAPE '\\' OR std_names LIKE ?1 ESCAPE '\\' "
            "ORDER BY std_name LIMIT ?2",
            (like_pattern(query), clamp(limit, 50))
        ).fetchall())


def get_person(pid: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
        if not row:
            return None
        variants = r(c.execute(
            "SELECT name_text, is_orig FROM name_index WHERE ssrq_id=? "
            "ORDER BY is_orig DESC, name_text LIMIT ?",
            (pid, VARIANT_LIMIT)
        ).fetchall())
        return dict(row) | {"name_variants": variants}


def get_persons_by_year(year_from: int, year_to: int, limit=100) -> list[dict]:
    """Persons whose attested life span overlaps the given range (inclusive)."""
    with conn() as c:
        return r(c.execute(
            f"SELECT {PERSON_BRIEF} FROM persons "
            "WHERE first_year IS NOT NULL AND last_year IS NOT NULL "
            "AND first_year <= ? AND last_year >= ? "
            "ORDER BY first_year, id LIMIT ?",
            (year_to, year_from, clamp(limit, 100))
        ).fetchall())


# ── Orgs ──────────────────────────────────────────────────────────────────────

ORG_BRIEF = "id, label, std_name, surname, alias_of, org_type"


def search_orgs(query: str, limit=50) -> list[dict]:
    with conn() as c:
        return r(c.execute(
            f"SELECT {ORG_BRIEF} FROM orgs "
            "WHERE std_name LIKE ?1 ESCAPE '\\' OR label LIKE ?1 ESCAPE '\\' "
            "ORDER BY std_name LIMIT ?2",
            (like_pattern(query), clamp(limit, 50))
        ).fetchall())


def get_org(oid: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute("SELECT * FROM orgs WHERE id=?", (oid,)).fetchone()
        if not row:
            return None
        variants = r(c.execute(
            "SELECT name_text, is_orig FROM name_index WHERE ssrq_id=? "
            "ORDER BY is_orig DESC, name_text LIMIT ?",
            (oid, VARIANT_LIMIT)
        ).fetchall())
        return dict(row) | {"name_variants": variants}


def org_index(limit=ORG_INDEX_LIMIT) -> dict:
    """Brief index of the organisation authority file. Says so when it is truncated,
    rather than silently returning a prefix of the register."""
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
        rows = r(c.execute(
            "SELECT id, label, std_name, org_type FROM orgs ORDER BY id LIMIT ?",
            (clamp(limit, ORG_INDEX_LIMIT, cap=ORG_INDEX_LIMIT),)
        ).fetchall())
    out = {"total": total, "returned": len(rows), "truncated": len(rows) < total,
           "orgs": rows}
    if out["truncated"]:
        out["note"] = (f"Showing the first {len(rows)} of {total} organisations. "
                       "Use search_orgs(query) to reach the rest.")
    return out


# ── Name index ────────────────────────────────────────────────────────────────

def search_name_index(query: str, type_filter: Optional[str] = None, limit=50) -> list[dict]:
    """Search every name variant on record. `type_filter` restricts to 'person' or
    'org'; anything else searches both. Every row carries `kind`, so the shape does
    not depend on the filter."""
    pat = like_pattern(query)
    n = clamp(limit, 50)
    person_sql = (
        "SELECT ni.name_text, ni.ssrq_id, ni.is_orig, p.label AS label, "
        "p.std_name AS std_name, 'person' AS kind "
        "FROM name_index ni JOIN persons p ON p.id = ni.ssrq_id "
        "WHERE ni.name_text LIKE ? ESCAPE '\\'"
    )
    org_sql = (
        "SELECT ni.name_text, ni.ssrq_id, ni.is_orig, o.label AS label, "
        "o.std_name AS std_name, 'org' AS kind "
        "FROM name_index ni JOIN orgs o ON o.id = ni.ssrq_id "
        "WHERE ni.name_text LIKE ? ESCAPE '\\'"
    )
    order = " ORDER BY is_orig DESC, std_name LIMIT ?"
    with conn() as c:
        if type_filter == "person":
            rows = c.execute(person_sql + order, (pat, n)).fetchall()
        elif type_filter == "org":
            rows = c.execute(org_sql + order, (pat, n)).fetchall()
        else:
            rows = c.execute(
                f"{person_sql} UNION ALL {org_sql}{order}", (pat, pat, n)
            ).fetchall()
    return r(rows)


def get_name_variants(id_: str, limit=VARIANT_LIMIT) -> list[dict]:
    """All name variants for a given person or org id."""
    with conn() as c:
        return r(c.execute(
            "SELECT name_text, ssrq_id, is_orig FROM name_index WHERE ssrq_id=? "
            "ORDER BY is_orig DESC, name_text LIMIT ?",
            (id_, clamp(limit, VARIANT_LIMIT, cap=VARIANT_LIMIT))
        ).fetchall())


# ── Relationships ─────────────────────────────────────────────────────────────

def related_persons(pid: str) -> dict[str, Any]:
    """Resolve a person's spouse / parent / organisation / place links in one call."""
    with conn() as c:
        person = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
        if not person:
            return {"error": f"Person '{pid}' not found."}

        def resolve(table: str, columns: str, ids_str: Optional[str]) -> list[dict]:
            ids = _split_ids(ids_str)
            if not ids:
                return []
            ids = ids[:MAX_LIMIT]
            placeholders = ",".join("?" * len(ids))
            return r(c.execute(
                f"SELECT {columns} FROM {table} WHERE id IN ({placeholders})", ids
            ).fetchall())

        out = {
            "person":        dict(person),
            "spouses":       resolve("persons", PERSON_BRIEF, person["spouse_ids"]),
            "mothers":       resolve("persons", PERSON_BRIEF, person["mother_ids"]),
            "fathers":       resolve("persons", PERSON_BRIEF, person["father_ids"]),
            "organisations": resolve("orgs", ORG_BRIEF, person["org_ids"]),
        }

        # Place ids point at the SSRQ place authority, which this database only
        # carries if the ETL imported it. Return the bare ids rather than joining
        # them against `persons`, which would silently yield nothing.
        loc_ids = _split_ids(person["loc_ids"])
        place_table = next((t for t in ("places", "locations") if _table_exists(c, t)), None)
        if place_table and loc_ids:
            out["places"] = resolve(place_table, "*", person["loc_ids"])
        else:
            out["places"] = [{"id": i} for i in loc_ids]
            if loc_ids:
                out["places_note"] = ("No place authority table in this database; "
                                      "only the referenced ids are returned.")
        return out
