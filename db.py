"""db.py — read-only SQLite helpers for the SSRQ MCP server."""
import re
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Optional

import corpus
import vectorstore

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


# The chunk and vector tables are the shared ones in vectorstore.SCHEMA_SQL
# (chunks.parent_id, embedding_runs.n_parents). Until #18 this repo had its
# own copy with chunks.doc_id; migrate_parent_id.py renames a database built
# with it. Kept separate from SCHEMA_SQL so a database built before the
# editions were ingested can be upgraded in place rather than rebuilt.
EMBEDDING_SCHEMA_SQL = vectorstore.SCHEMA_SQL


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


# ── Semantic search: the shared vectorstore, with this corpus's join ─────────
#
# The columns are this corpus's, not Königsfelden's: short_id and source came
# across with the copied machinery and do not exist here. A missing column is
# not caught until a query runs, and inside a tool it reaches the caller as a
# bare "Error executing tool search_semantic".
_JOIN_SQL = (
    "SELECT c.chunk_id,c.parent_id,c.chunk_index,c.char_start,c.char_end,c.text,"
    "e.title,e.canton,e.volume,e.origin_from AS year,e.origin_to,e.lang,e.place,e.url "
    "FROM chunks c JOIN documents e ON e.id=c.parent_id "
    "WHERE c.chunk_id IN ({placeholders})"
)


def passage_body(text: str, title, year) -> str:
    """The passage without its head line (fleet rule, ch-h-bot#555).

    Every passage carries `title origin_from` on its first line — for the
    vector, which otherwise has no idea which document or century a window
    belongs to. The reader does not need it there: the hit carries title and
    year as fields, the bot's citation line shows them, and a snippet that
    starts with them repeats the line above it, puts the head instead of the
    text into the link's text fragment and hides the half-word a window may
    start with from the bot's trimming (thodel/eperiodica_mcp#10). Only a
    text that starts with exactly this document's head, built by the same
    function that wrote it, is cut; a passage without one is left alone.
    Offsets and vectors are untouched.
    """
    head = corpus.head_line(title, year)
    if head and text.startswith(head + "\n"):
        return text[len(head) + 1:]
    return text


def _row_to_hit(row, chunk_id: str, score: float, year_from=None, year_to=None):
    """One joined row as a hit, or None when the year filter drops it."""
    year = row["year"]
    if year_from is not None and (year is None or year < year_from):
        return None
    if year_to is not None and (year is None or year > year_to):
        return None
    return {
        "id": row["parent_id"],
        "chunk_id": chunk_id,
        "title": row["title"],
        "canton": row["canton"],
        "volume": row["volume"],
        "year": year,
        "year_to": row["origin_to"],
        "lang": row["lang"],
        "place": row["place"],
        "url": row["url"],
        "snippet": passage_body(row["text"], row["title"], year),
        "score": score,
        "chunk_index": row["chunk_index"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
    }


def search_semantic(query_vector, limit=20, model=None, year_from=None,
                    year_to=None, per_parent=2):
    """Passages closest in meaning to an already-embedded query.

    ``per_parent`` caps how many passages one document may contribute, so a
    long document cannot fill the result set and crowd out the other documents
    that answer the question. ``year_from``/``year_to`` restrict to a period,
    which for this corpus is often the point of the question.
    """
    return vectorstore.search(
        conn, _DB_PATH, query_vector,
        join_sql=_JOIN_SQL,
        row_to_hit=lambda r, cid, sc: _row_to_hit(r, cid, sc, year_from, year_to),
        limit=clamp(limit, 20), model=model, per_parent=per_parent)


def semantic_stats() -> dict:
    """Coverage of the semantic index, and the runs that produced it."""
    return vectorstore.stats(conn, corpus.PARENT_TABLE)


def warm_semantic_index(model):
    """Load the vectors now and report what was loaded, or why it could not be."""
    return vectorstore.warm(conn, _DB_PATH, model)


def set_db_path(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path
    vectorstore.clear_cache()      # the matrix is per (path, model)


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
    """The ids in a relation column, whatever the stored form.

    ssrq_parse_ttl.py writes the five relation columns as JSON arrays —
    '[]', '["loc008882"]' — and an older build wrote comma-joined ids
    ('per000001,per000002'). Only the comma form was split here, so a JSON
    column went through as ONE id: related_persons answered
    places=[{"id": "[]"}] and organisations=[] for a person whose org_ids is
    '["org000039"]' (measured on the live server, 14./19.10.2026, issue #15).
    """
    text = (ids_str or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(i).strip() for i in parsed if str(i).strip()]
    return [i.strip() for i in text.split(",") if i.strip()]


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone() is not None


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats() -> dict:
    """Corpus statistics, with year_min/year_max describing the EVIDENCE.

    year_min/year_max used to come from the person authority file, which
    meant "the years persons are attested", not "the years the documents
    cover" — and consumers comparing them against the documented edition span
    (1050-1846) read the difference as drift (ch-h-bot#284, #11). A person
    can be born before the earliest document and attested after the last one,
    so the two ranges legitimately differ.

    year_min/year_max now describe the documents — what this server can serve
    as evidence — and the authority file's span moves to its own keys. When no
    documents are ingested, the person range fills in, labelled as such.
    """
    with conn() as c:
        out = {
            "n_persons":    c.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
            "n_orgs":       c.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
            "n_name_index": c.execute("SELECT COUNT(*) FROM name_index").fetchone()[0],
            "persons_year_min": c.execute(
                "SELECT MIN(first_year) FROM persons "
                "WHERE first_year IS NOT NULL").fetchone()[0],
            "persons_year_max": c.execute(
                "SELECT MAX(last_year) FROM persons "
                "WHERE last_year IS NOT NULL").fetchone()[0],
        }
        has_documents = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='documents'").fetchone()
        doc_min = doc_max = None
        if has_documents:
            doc_min = c.execute(
                "SELECT MIN(origin_from) FROM documents "
                "WHERE origin_from IS NOT NULL").fetchone()[0]
            doc_max = c.execute(
                "SELECT MAX(COALESCE(origin_to, origin_from)) FROM documents "
                "WHERE origin_from IS NOT NULL").fetchone()[0]
        if doc_min is not None:
            out["year_min"], out["year_max"] = doc_min, doc_max
            out["year_range_of"] = "documents"
        else:
            out["year_min"] = out["persons_year_min"]
            out["year_max"] = out["persons_year_max"]
            out["year_range_of"] = "persons"
        return out


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
