"""db.py — Read-only SQLite access for the SSRQ MCP server."""
import sqlite3
from typing import Any, Optional

DB_PATH: str = "/data/ssrq.db"


def set_db_path(path: str) -> None:
    global DB_PATH
    DB_PATH = path


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def r(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict keyed by column name."""
    return dict(row)


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats() -> dict[str, int]:
    with _conn() as c:
        return {
            "persons":    c.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
            "orgs":       c.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
            "name_index": c.execute("SELECT COUNT(*) FROM name_index").fetchone()[0],
        }


# ── Persons ───────────────────────────────────────────────────────────────────

def search_persons(query: str, limit: int = 50) -> list[dict]:
    pat = f"%{query}%"
    with _conn() as c:
        rows = c.execute(
            """SELECT id, label, std_name, forename, surname, sex,
                      first_year, last_year, years, orig_names
               FROM persons
               WHERE std_name LIKE ? OR label LIKE ? OR orig_names LIKE ?
               ORDER BY std_name
               LIMIT ?""",
            (pat, pat, pat, limit),
        ).fetchall()
    return [r(row) for row in rows]


def get_person(pid: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            """SELECT id, uri, etype, label, label_lang, std_name,
                      forename, surname, sex, first_year, last_year, years,
                      org_ids, spouse_ids, mother_ids, father_ids, loc_ids,
                      orig_names, std_names
               FROM persons WHERE id = ?""",
            (pid,),
        ).fetchone()
        return r(row) if row else None


# ── Orgs ──────────────────────────────────────────────────────────────────────

def search_orgs(query: str, limit: int = 50) -> list[dict]:
    pat = f"%{query}%"
    with _conn() as c:
        rows = c.execute(
            """SELECT id, label, std_name, surname, alias_of, org_type
               FROM orgs
               WHERE std_name LIKE ? OR label LIKE ?
               ORDER BY std_name
               LIMIT ?""",
            (pat, pat, limit),
        ).fetchall()
    return [r(row) for row in rows]


def get_org(oid: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            """SELECT id, uri, etype, label, std_name, surname,
                      alias_of, org_type
               FROM orgs WHERE id = ?""",
            (oid,),
        ).fetchone()
        return r(row) if row else None


# ── Name index ────────────────────────────────────────────────────────────────

def search_name_index(
    query: str,
    type_filter: Optional[str],
    limit: int = 50,
) -> list[dict]:
    """Search all name variants. Optionally restrict to 'person' or 'org'."""
    pat = f"%{query}%"
    with _conn() as c:
        if type_filter == "person":
            rows = c.execute(
                """SELECT ni.name_text, ni.ssrq_id, ni.is_orig,
                          p.label, p.std_name
                   FROM name_index ni
                   JOIN persons p ON p.id = ni.ssrq_id
                   WHERE ni.name_text LIKE ?
                   ORDER BY ni.is_orig DESC, p.std_name
                   LIMIT ?""",
                (pat, limit),
            ).fetchall()
        elif type_filter == "org":
            rows = c.execute(
                """SELECT ni.name_text, ni.ssrq_id, ni.is_orig,
                          o.label, o.std_name
                   FROM name_index ni
                   JOIN orgs o ON o.id = ni.ssrq_id
                   WHERE ni.name_text LIKE ?
                   ORDER BY ni.is_orig DESC, o.std_name
                   LIMIT ?""",
                (pat, limit),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT ni.name_text, ni.ssrq_id, ni.is_orig,
                          p.label AS label, p.std_name AS std_name, 'person' AS kind
                   FROM name_index ni
                   JOIN persons p ON p.id = ni.ssrq_id
                   WHERE ni.name_text LIKE ?
                   UNION ALL
                   SELECT ni.name_text, ni.ssrq_id, ni.is_orig,
                          o.label, o.std_name, 'org' AS kind
                   FROM name_index ni
                   JOIN orgs o ON o.id = ni.ssrq_id
                   WHERE ni.name_text LIKE ?
                   ORDER BY is_orig DESC, std_name
                   LIMIT ?""",
                (pat, pat, limit),
            ).fetchall()
    return [r(row) for row in rows]


def get_name_variants(id_: str) -> list[dict]:
    """All name variants for a given person or org id."""
    with _conn() as c:
        rows = c.execute(
            """SELECT name_text, ssrq_id, is_orig
               FROM name_index
               WHERE ssrq_id = ?
               ORDER BY is_orig DESC, name_text""",
            (id_,),
        ).fetchall()
    return [r(row) for row in rows]


# ── Relationships ─────────────────────────────────────────────────────────────

def related_persons(pid: str) -> dict[str, Any]:
    """Gather spouse/family/org/place links for a person."""
    with _conn() as c:
        person = c.execute(
            "SELECT * FROM persons WHERE id = ?", (pid,)
        ).fetchone()
        if not person:
            return {"error": f"Person '{pid}' not found."}

        def resolve_ids(ids_str: Optional[str]) -> list[dict]:
            if not ids_str:
                return []
            ids = [i.strip() for i in ids_str.split(",") if i.strip()]
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            rows = c.execute(
                f"SELECT * FROM persons WHERE id IN ({placeholders})", ids
            ).fetchall()
            return [r(row) for row in rows]

        def resolve_orgs(ids_str: Optional[str]) -> list[dict]:
            if not ids_str:
                return []
            ids = [i.strip() for i in ids_str.split(",") if i.strip()]
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            rows = c.execute(
                f"SELECT * FROM orgs WHERE id IN ({placeholders})", ids
            ).fetchall()
            return [r(row) for row in rows]

        def resolve_locs(ids_str: Optional[str]) -> list[dict]:
            if not ids_str:
                return []
            ids = [i.strip() for i in ids_str.split(",") if i.strip()]
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            rows = c.execute(
                f"""SELECT id, label, std_name, label_lang, first_year, last_year, years
                    FROM persons WHERE id IN ({placeholders})""", ids
            ).fetchall()
            return [r(row) for row in rows]

        return {
            "person": r(person),
            "spouses":    resolve_ids(person["spouse_ids"]),
            "mothers":    resolve_ids(person["mother_ids"]),
            "fathers":    resolve_ids(person["father_ids"]),
            "organisations": resolve_orgs(person["org_ids"]),
            "places":     resolve_locs(person["loc_ids"]),
        }
