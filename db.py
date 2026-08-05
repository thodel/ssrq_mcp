"""db.py — read-only SQLite helpers for the SSRQ MCP server."""
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
"""


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
