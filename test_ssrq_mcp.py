#!/usr/bin/env python3
"""
test_ssrq_mcp.py — test suite for the SSRQ MCP server.

Runs two ways. Under pytest, failures fail the run; the CLI keeps the grouped
output and sets the exit code.

    pytest test_ssrq_mcp.py                                     # unit tests only
    SSRQ_DB=/data/ssrq.db pytest test_ssrq_mcp.py               # + DB tests
    SSRQ_SERVER=http://localhost:8002 pytest test_ssrq_mcp.py   # + server tests

    python test_ssrq_mcp.py --unit
    python test_ssrq_mcp.py --db /data/ssrq.db
    python test_ssrq_mcp.py --server http://localhost:8002
    python test_ssrq_mcp.py --unit --db /data/ssrq.db --server http://localhost:8002

Unit tests build their own throwaway database, so they need no setup. Tests
needing the real DB or a live server skip when it isn't configured.
Requires pytest (see requirements-dev.txt).
"""
import argparse, json, os, sqlite3, sys, tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Helpers ───────────────────────────────────────────────────────────────────

RED   = "\033[91m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RESET = "\033[0m"

def ok(msg):   print(f"{GREEN}✅ {msg}{RESET}")
def fail(msg): print(f"{RED}❌ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}⚠️  {msg}{RESET}")
def info(msg): print(f"   {msg}")

class Checks:
    """Collects several checks so one run reports them all, then fails as a unit.

    Not named Test* — pytest would try to collect it as a test class.
    """
    def __init__(self): self.passed = self.failed = 0; self.failures = []
    def check(self, cond, msg):
        if cond:
            self.passed += 1
            ok(msg)
        else:
            self.failed += 1
            self.failures.append(msg)
            fail(msg)
    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'─'*50}")
        print(f"Ran {total} checks: {GREEN}{self.passed} passed{RESET}", end="")
        if self.failed: print(f", {RED}{self.failed} failed{RESET}", end="")
        print()
        return self.failed == 0
    def assert_ok(self):
        """Raise if any check failed — this is what makes the suite real under pytest."""
        self.summary()
        if self.failed:
            raise AssertionError(
                f"{self.failed} of {self.passed + self.failed} checks failed:\n  - "
                + "\n  - ".join(self.failures)
            )


@pytest.fixture
def db_path():
    """Path to a built ssrq.db, from the SSRQ_DB env var. Empty → DB tests skip."""
    return os.environ.get("SSRQ_DB", "")

@pytest.fixture
def base_url():
    """Base URL of a running server, from SSRQ_SERVER. Empty → server tests skip."""
    return os.environ.get("SSRQ_SERVER", "")


# ── Synthetic fixture database ────────────────────────────────────────────────

PERSONS = [
    # id, uri, etype, label, label_lang, std_name, forename, surname, sex,
    # first_year, last_year, years, org_ids, spouse_ids, mother_ids, father_ids,
    # loc_ids, orig_names, std_names
    ("per000001", "u1", "person", "Heinrich von Brugg", "de", "Brugg, Heinrich von",
     "Heinrich", "Brugg", "m", 1330, 1360, "1330-1360", "org000001", "per000002",
     "", "", "loc000001,loc000002", "Heinricus de Brugga", "Brugg, Heinrich von"),
    ("per000002", "u2", "person", "Anna 100% Sicher", "de", "Sicher, Anna 100%",
     "Anna", "Sicher", "f", 1340, 1390, "1340-1390", "", "per000001", "", "", "", "", ""),
    ("per000003", "u3", "person", "Hans_Meier", "de", "Meier, Hans_",
     "Hans", "Meier", "m", 1500, 1540, "1500-1540", "", "", "", "", "", "", ""),
    ("per000004", "u4", "person", "Ulrich Zwingli", "de", "Zwingli, Ulrich",
     "Ulrich", "Zwingli", "m", 1484, 1531, "1484-1531", "", "", "", "", "", "", ""),
]

ORGS = [
    ("org000001", "o1", "org", "Rat von Bern",  "Bern, Rat",  "Bern",  "", "council"),
    ("org000002", "o2", "org", "Stadt Brugg",   "Brugg, Stadt", "Brugg", "", "city"),
]

NAME_INDEX = [
    ("Heinricus de Brugga", "per000001", 1),
    ("Heinrich von Brugg",  "per000001", 0),
    ("Anna 100% Sicher",    "per000002", 0),
    ("Rat von Bern",        "org000001", 0),
    ("Stadt Brugg",         "org000002", 1),
]


def make_fixture_db(path):
    """Build a small database matching db.SCHEMA_SQL."""
    import db as db_module
    con = sqlite3.connect(path)
    con.executescript(db_module.SCHEMA_SQL)
    con.executemany(f"INSERT INTO persons VALUES ({','.join('?' * 19)})", PERSONS)
    con.executemany(f"INSERT INTO orgs VALUES ({','.join('?' * 8)})", ORGS)
    con.executemany("INSERT INTO name_index VALUES (?,?,?)", NAME_INDEX)
    con.commit(); con.close()
    db_module.set_db_path(path)
    return db_module


# ── 1. Unit tests — query layer ───────────────────────────────────────────────

def test_rows_are_dicts_not_tuples():
    """Every query helper must return dicts — a missing row_factory turns dict(row)
    into a TypeError at the first search."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")
        rows = db.search_persons("Heinrich")
        tr.check(rows and isinstance(rows[0], dict), f"search_persons returns dicts (got {rows[:1]})")
        tr.check(rows[0]["id"] == "per000001", "person row is keyed by column name")
        tr.check(isinstance(db.stats(), dict) and db.stats()["n_persons"] == 4,
                 "stats() counts the fixture rows")
        p = db.get_person("per000001")
        tr.check(isinstance(p, dict) and p["std_name"] == "Brugg, Heinrich von",
                 "get_person returns the record")
        tr.check([v["name_text"] for v in p["name_variants"]][0] == "Heinricus de Brugga",
                 "get_person attaches name variants, original spelling first")
        tr.check(db.get_person("per999999") is None, "unknown person id returns None")
        tr.check(db.get_org("org999999") is None, "unknown org id returns None")
    tr.assert_ok()


def test_like_wildcards_are_escaped():
    """A '%' or '_' in a search query must match itself, not act as a wildcard."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")
        names = lambda rows: sorted(r["label"] for r in rows)

        tr.check(names(db.search_persons("%")) == ["Anna 100% Sicher"],
                 "'%' matches a literal percent, not every row")
        tr.check(names(db.search_persons("_")) == ["Hans_Meier"],
                 "'_' matches a literal underscore, not any character")
        tr.check(names(db.search_persons("100%")) == ["Anna 100% Sicher"],
                 "percent inside a query is literal")
        tr.check(db.search_persons("%Zwingli%") == [],
                 "caller-supplied wildcards do not expand")
        tr.check(names(db.search_persons("Heinrich")) == ["Heinrich von Brugg"],
                 "ordinary substring search still works")
        tr.check(db.search_orgs("%") == [], "search_orgs escapes wildcards too")
        tr.check(len(db.search_name_index("%")) == 1,
                 "search_name_index escapes wildcards too")
        tr.check(db.like_pattern("a%b_c") == "%a\\%b\\_c%", "like_pattern escapes both wildcards")
    tr.assert_ok()


def test_limits_are_clamped():
    """LIMIT -1 is unbounded in SQLite, so a negative limit must fall back."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")
        tr.check(db.clamp(-1, 50) == 50, "negative limit falls back to the default")
        tr.check(db.clamp(0, 50) == 50, "zero limit falls back to the default")
        tr.check(db.clamp("many", 50) == 50, "non-numeric limit falls back to the default")
        tr.check(db.clamp(10**9, 50) == db.MAX_LIMIT, f"huge limit capped at {db.MAX_LIMIT}")
        tr.check(db.clamp(5, 50) == 5, "a sane limit is passed through")
        tr.check(db.clamp_offset(-3) == 0, "negative offset clamps to 0")

        tr.check(len(db.list_persons(-1)) == 4, "list_persons(-1) is bounded, not unbounded")
        tr.check(len(db.list_persons(2)) == 2, "list_persons honours a sane limit")
        first, second = db.list_persons(2, 0), db.list_persons(2, 2)
        tr.check({r["id"] for r in first}.isdisjoint({r["id"] for r in second}),
                 "list_persons pages without overlap")
        tr.check(len(db.search_persons("e", limit=-5)) <= db.MAX_LIMIT,
                 "search_persons(-5) is bounded")
    tr.assert_ok()


def test_name_index_shape_is_stable():
    """Filtered and unfiltered searches must return the same columns, `kind` included."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")

        both = db.search_name_index("Brugg")
        tr.check({r["kind"] for r in both} == {"person", "org"},
                 f"unfiltered search covers persons and orgs (got {[r['kind'] for r in both]})")

        persons_only = db.search_name_index("Brugg", "person")
        tr.check(persons_only and all(r["ssrq_id"].startswith("per") for r in persons_only),
                 "type_filter='person' returns only persons")
        orgs_only = db.search_name_index("Brugg", "org")
        tr.check(orgs_only and all(r["ssrq_id"].startswith("org") for r in orgs_only),
                 "type_filter='org' returns only orgs")
        tr.check(set(persons_only[0]) == set(both[0]) == set(orgs_only[0]),
                 "filtered and unfiltered rows have identical keys")
        tr.check(all("kind" in r for r in persons_only + orgs_only),
                 "`kind` is present even when filtered")

        variants = db.get_name_variants("per000001")
        tr.check([v["name_text"] for v in variants] ==
                 ["Heinricus de Brugga", "Heinrich von Brugg"],
                 "get_name_variants orders original spellings first")
        tr.check(db.get_name_variants("per999999") == [], "unknown id yields no variants")
    tr.assert_ok()


def test_related_persons_resolves_links():
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")
        rel = db.related_persons("per000001")

        tr.check(rel["person"]["id"] == "per000001", "the person itself is returned")
        tr.check([s["id"] for s in rel["spouses"]] == ["per000002"], "spouse ids resolve to records")
        tr.check([o["id"] for o in rel["organisations"]] == ["org000001"],
                 "org ids resolve against the orgs table")
        tr.check(rel["mothers"] == [] and rel["fathers"] == [], "empty relation columns yield []")
        # loc_ids point at a place authority this database does not carry: the ids
        # must come back as ids, never as rows accidentally joined from `persons`.
        tr.check([p["id"] for p in rel["places"]] == ["loc000001", "loc000002"],
                 f"place ids are returned unresolved (got {rel['places']})")
        tr.check("places_note" in rel, "unresolved places are flagged with a note")
        tr.check(all(set(p) == {"id"} for p in rel["places"]),
                 "unresolved places carry no borrowed person columns")

        missing = db.related_persons("per999999")
        tr.check("error" in missing, "unknown person returns an error object, not a crash")
    tr.assert_ok()


def test_persons_by_year_overlap():
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")
        ids = lambda rows: sorted(r["id"] for r in rows)
        tr.check(ids(db.get_persons_by_year(1335, 1345)) == ["per000001", "per000002"],
                 "a range inside both life spans returns both persons")
        tr.check(ids(db.get_persons_by_year(1490, 1495)) == ["per000004"],
                 "a narrow range returns only the overlapping person")
        tr.check(db.get_persons_by_year(1600, 1650) == [],
                 "a range outside every life span returns nothing")
    tr.assert_ok()


def test_org_index_reports_truncation():
    """ssrq://orgs must say when it is only showing a prefix of the register."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")
        full = db.org_index()
        tr.check(full["total"] == 2 and full["returned"] == 2, "full index returns everything")
        tr.check(full["truncated"] is False, "full index is not flagged truncated")
        tr.check("note" not in full, "no truncation note when nothing is cut")

        cut = db.org_index(limit=1)
        tr.check(cut["total"] == 2 and cut["returned"] == 1, "truncated index reports both counts")
        tr.check(cut["truncated"] is True, "truncation is flagged")
        tr.check("search_orgs" in cut.get("note", ""), "note points at search_orgs")
    tr.assert_ok()


def test_connection_is_read_only():
    """The server must never be able to write to the corpus."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/ssrq.db")
        with db.conn() as c:
            try:
                c.execute("DELETE FROM persons")
                tr.check(False, "a write through db.conn() was accepted")
            except sqlite3.OperationalError as e:
                tr.check(True, f"writes are rejected ({e})")
        tr.check(db.stats()["n_persons"] == 4, "corpus is intact after the attempted write")
    tr.assert_ok()


def test_server_module_registers_tools():
    """server.py must import without reading sys.argv, and expose every tool."""
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    import anyio
    import server as server_module

    tr = Checks()
    expected = {"corpus_stats", "list_persons", "search_persons", "get_person",
                "get_persons_by_year", "search_orgs", "get_org", "search_name_index",
                "get_name_variants", "related_persons"}
    names = {t.name for t in anyio.run(server_module.mcp.list_tools)}
    tr.check(not expected - names, f"all tools registered (missing: {sorted(expected - names)})")

    args = server_module.parse_args(["--db", "/tmp/x.db", "--port", "9999"])
    tr.check(args.db == "/tmp/x.db" and args.port == 9999, "CLI flags override the env defaults")

    # The endpoint path must match the public path exactly, however it is written:
    # a sub-path deployment 404s when the app is mounted at /mcp while nginx
    # forwards /mcp/ssrq/mcp.
    n = server_module.normalise_path
    tr.check(n("/mcp/ssrq/mcp") == "/mcp/ssrq/mcp", "an already-correct path is unchanged")
    tr.check(n("mcp/ssrq/mcp") == "/mcp/ssrq/mcp", "a missing leading slash is added")
    tr.check(n("/mcp/ssrq/mcp/") == "/mcp/ssrq/mcp", "a trailing slash is dropped")
    tr.check(n("") == "/mcp" and n(None) == "/mcp", "an empty path falls back to /mcp")
    tr.check(server_module.parse_args([]).http_path == "/mcp", "default endpoint path is /mcp")
    tr.check(server_module.parse_args(["--http-path", "mcp/ssrq/mcp/"]).http_path
             == "/mcp/ssrq/mcp", "--http-path is normalised on the way in")
    tr.assert_ok()


# ── 2. DB tests — against the real corpus ─────────────────────────────────────

def test_db_layer_against_real_db(db_path):
    """db.py against a real ssrq.db, including hostile inputs."""
    if not db_path or not os.path.exists(db_path):
        pytest.skip(f"ssrq.db not found at {db_path!r} — set SSRQ_DB or pass --db")

    import db as db_module
    db_module.set_db_path(db_path)
    tr = Checks()

    s = db_module.stats()
    info(f"persons={s['n_persons']} orgs={s['n_orgs']} name_index={s['n_name_index']} "
         f"years={s['year_min']}–{s['year_max']}")
    tr.check(s["n_persons"] >= 20000, f"persons: >=20000 (got {s['n_persons']})")
    tr.check(s["n_orgs"] >= 6000, f"orgs: >=6000 (got {s['n_orgs']})")
    tr.check(s["n_name_index"] >= 100000, f"name variants: >=100000 (got {s['n_name_index']})")

    # limits are clamped, never unbounded
    tr.check(len(db_module.list_persons(-1)) <= db_module.MAX_LIMIT, "list_persons(-1) is bounded")
    tr.check(len(db_module.list_persons(10**9)) <= db_module.MAX_LIMIT,
             f"list_persons(huge) capped at {db_module.MAX_LIMIT}")

    for query in ["Heinrich", "%", "_", "100%", "O'Brien", 'quote"mark']:
        try:
            res = db_module.search_persons(query, 5)
            tr.check(isinstance(res, list), f"search_persons({query!r}) returned a list")
        except Exception as e:
            tr.check(False, f"search_persons({query!r}) raised {type(e).__name__}: {e}")

    for query in ["Johann", "%"]:
        for tf in (None, "person", "org"):
            try:
                res = db_module.search_name_index(query, tf, 5)
                tr.check(all("kind" in r for r in res),
                         f"search_name_index({query!r}, {tf!r}) rows carry `kind`")
            except Exception as e:
                tr.check(False, f"search_name_index({query!r}, {tf!r}) raised {type(e).__name__}: {e}")

    # A real person, resolved end to end
    sample = db_module.list_persons(1)
    if sample:
        pid = sample[0]["id"]
        person = db_module.get_person(pid)
        tr.check(person is not None and person["id"] == pid, f"get_person({pid!r}) round-trips")
        rel = db_module.related_persons(pid)
        tr.check("person" in rel, f"related_persons({pid!r}) returns a person block")

    # Someone with links, so the resolution path is actually exercised
    with db_module.conn() as c:
        row = c.execute(
            "SELECT id FROM persons WHERE spouse_ids IS NOT NULL AND spouse_ids != '' LIMIT 1"
        ).fetchone()
    if row:
        rel = db_module.related_persons(row[0])
        info(f"{row[0]}: {len(rel['spouses'])} spouse(s), {len(rel['organisations'])} org(s)")
        tr.check(isinstance(rel["spouses"], list), "spouse links resolve to a list of records")
    else:
        warn("no person with spouse_ids in this database — link resolution not exercised")

    tr.check(db_module.get_person("per999999") is None, "an unknown id returns None")
    tr.assert_ok()


# ── 3. Server integration test ────────────────────────────────────────────────

def _tool_payload(result):
    """Unwrap a CallToolResult into a Python object."""
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured.get("result", structured)
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try: return json.loads(text)
            except json.JSONDecodeError: return text
    return None


def test_server(base_url):
    """Drive the running server over streamable HTTP using the official client."""
    if not base_url:
        pytest.skip("no server URL — set SSRQ_SERVER or pass --server")
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as e:
        pytest.skip(f"mcp client library not available: {e}")

    tr = Checks()
    expected = {"corpus_stats", "list_persons", "search_persons", "get_person",
                "get_persons_by_year", "search_orgs", "get_org", "search_name_index",
                "get_name_variants", "related_persons"}
    url = base_url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"

    async def exercise():
        # Streamable HTTP is a session protocol: the server hands out a session id on
        # initialize and expects it on every later POST. Hand-rolled POSTs cannot work.
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                names = {t.name for t in (await session.list_tools()).tools}
                tr.check(bool(names), f"tools/list returned {len(names)} tools")
                missing = expected - names
                tr.check(not missing, f"all expected tools exposed (missing: {sorted(missing)})")

                stats = _tool_payload(await session.call_tool("corpus_stats", {}))
                tr.check(isinstance(stats, dict) and stats.get("n_persons", 0) > 0,
                         f"corpus_stats returns persons (got {stats})")

                for tool, args in [
                    ("search_persons",     {"query": "Heinrich", "limit": 5}),
                    ("search_orgs",        {"query": "Stadt", "limit": 5}),
                    ("search_name_index",  {"query": "Johann", "limit": 5}),
                    ("list_persons",       {"limit": 3}),
                    ("get_persons_by_year", {"year_from": 1400, "year_to": 1450, "limit": 5}),
                ]:
                    res = await session.call_tool(tool, args)
                    tr.check(not res.is_error, f"{tool} call succeeded")
                    tr.check(_tool_payload(res) is not None, f"{tool} returned a payload")

                # Hostile input must come back as data, not a transport error
                res = await session.call_tool("search_persons", {"query": "%", "limit": 3})
                tr.check(not res.is_error, "search_persons survives a bare wildcard")

                res = await session.call_tool("get_person", {"pid": "per999999"})
                payload = _tool_payload(res)
                tr.check(isinstance(payload, dict) and "error" in payload,
                         f"unknown person id returns an error object (got {payload})")

                res = await session.call_tool("get_persons_by_year",
                                              {"year_from": 1500, "year_to": 1400})
                payload = _tool_payload(res)
                tr.check(isinstance(payload, list) and "error" in payload[0],
                         "an inverted year range is rejected as data")

                resources = {str(r.uri) for r in (await session.list_resources()).resources}
                tr.check("ssrq://stats" in resources,
                         f"ssrq://stats resource listed (got {sorted(resources)})")

                templates = (await session.list_resource_templates()).resource_templates
                tr.check("ssrq://person/{pid}" in {t.uri_template for t in templates},
                         "ssrq://person/{pid} template listed")

                sample = _tool_payload(await session.call_tool("list_persons", {"limit": 1}))
                if sample:
                    read = await session.read_resource(f"ssrq://person/{sample[0]['id']}")
                    payload = json.loads(read.contents[0].text)
                    tr.check(payload.get("id") == sample[0]["id"],
                             "ssrq://person/{pid} resolves to that person's record")

    anyio.run(exercise)
    tr.assert_ok()


# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_run(label, fn, *fn_args):
    """Run one test in CLI mode, translating pytest outcomes into a bool."""
    print(f"\n{label}")
    try:
        fn(*fn_args)
        return True
    except pytest.skip.Exception as e:
        warn(f"skipped: {e}")
        return True
    except AssertionError as e:
        fail(str(e))
        return False
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SSRQ MCP test suite")
    ap.add_argument("--unit", action="store_true", help="Run unit tests")
    ap.add_argument("--db", default=os.environ.get("SSRQ_DB", ""), help="Path to ssrq.db")
    ap.add_argument("--server", default=os.environ.get("SSRQ_SERVER", ""), help="Server base URL")
    args = ap.parse_args()

    if not args.unit and not args.db and not args.server:
        ap.print_help()
        sys.exit(0)

    print(f"{'═'*50}")
    print("SSRQ MCP test suite")
    print(f"{'═'*50}")

    ok_all = True

    if args.unit:
        ok_all &= cli_run("[1] Unit: rows are dicts", test_rows_are_dicts_not_tuples)
        ok_all &= cli_run("[2] Unit: LIKE wildcards are escaped", test_like_wildcards_are_escaped)
        ok_all &= cli_run("[3] Unit: limits are clamped", test_limits_are_clamped)
        ok_all &= cli_run("[4] Unit: name index shape is stable", test_name_index_shape_is_stable)
        ok_all &= cli_run("[5] Unit: related_persons resolves links", test_related_persons_resolves_links)
        ok_all &= cli_run("[6] Unit: persons by year overlap", test_persons_by_year_overlap)
        ok_all &= cli_run("[7] Unit: org index reports truncation", test_org_index_reports_truncation)
        ok_all &= cli_run("[8] Unit: connection is read-only", test_connection_is_read_only)
        ok_all &= cli_run("[9] Unit: server registers its tools", test_server_module_registers_tools)

    if args.db:
        ok_all &= cli_run(f"[10] DB: db.py query layer ({args.db})",
                          test_db_layer_against_real_db, args.db)

    if args.server:
        ok_all &= cli_run(f"[11] Server: MCP integration ({args.server})", test_server, args.server)

    print(f"\n{'═'*50}")
    print(f"{GREEN}ALL PASSED{RESET}" if ok_all else f"{RED}FAILURES{RESET}")
    sys.exit(0 if ok_all else 1)


# ── The editions ──────────────────────────────────────────────────────────────
# Until these were added the server held an authority file and nothing else. The
# tests below are about the parts the TEI does not hand over: the document's own
# title, the date it was issued rather than the date it was published, and a
# language nobody declared.

import ingest_editio  # noqa: E402


TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xml:lang="de">
 <teiHeader>
  <fileDesc><titleStmt>
    <title>IX. Abteilung: Die Rechtsquellen des Kantons Freiburg</title>
  </titleStmt></fileDesc>
  <publicationStmt><idno type="ssrq">SSRQ-ZH-TEST-1-1</idno>
    <date type="electronic" when="2022-06-28"/></publicationStmt>
 </teiHeader>
 <text><body>
  <head xml:lang="de">Ordnung betreffend die Witwen</head>
  <origDate from="1446-01-19" to="1468-01-09"/>
  <p>Wir, der burgermeister und die raͤt der statt Zu̍rich, haben unns
     vereinbart und bekenndt, das die froͧwen nach unnser statt recht
     ußgericht werden soͤllen und nicht anders.</p>
 </body></text>
</TEI>"""


def _write(tmp_path, xml=TEI, name="SSRQ-ZH-TEST-1-1.xml"):
    volume = tmp_path / "data" / "ZH" / "ZH_TEST"
    volume.mkdir(parents=True, exist_ok=True)
    (volume / name).write_text(xml, encoding="utf-8")
    return tmp_path


def test_the_document_title_is_the_head_not_the_series(tmp_path):
    """<title> is the series — the same string for every file in a volume, so
    it identifies nothing in a footnote. The document's own title is <head>."""
    root = _write(tmp_path)
    row = ingest_editio.parse(
        root / "data/ZH/ZH_TEST/SSRQ-ZH-TEST-1-1.xml", root)

    assert row["title"] == "Ordnung betreffend die Witwen"
    assert "IX. Abteilung" not in row["title"]


def test_the_date_is_when_the_document_was_issued(tmp_path):
    """The same file carries <date type="electronic" when="2022-06-28"/>, the
    date the edition was published. Reading that dates a 1446 charter to 2022."""
    root = _write(tmp_path)
    row = ingest_editio.parse(
        root / "data/ZH/ZH_TEST/SSRQ-ZH-TEST-1-1.xml", root)

    assert row["origin_from"] == 1446
    assert row["origin_to"] == 1468


def test_the_canton_and_volume_come_from_the_path(tmp_path):
    root = _write(tmp_path)
    row = ingest_editio.parse(
        root / "data/ZH/ZH_TEST/SSRQ-ZH-TEST-1-1.xml", root)

    assert (row["canton"], row["volume"]) == ("ZH", "ZH_TEST")


def test_language_is_detected_not_read_from_the_header(tmp_path):
    """xml:lang on <TEI> is the language of the edition's metadata. Every
    Fribourg file says "de" whether the charter is German or French."""
    root = _write(tmp_path)
    row = ingest_editio.parse(
        root / "data/ZH/ZH_TEST/SSRQ-ZH-TEST-1-1.xml", root)
    assert row["lang"] == "de"

    french = TEI.replace(
        "<p>Wir, der burgermeister und die raͤt der statt Zu̍rich, haben unns\n"
        "     vereinbart und bekenndt, das die froͧwen nach unnser statt recht\n"
        "     ußgericht werden soͤllen und nicht anders.</p>",
        "<p>Nous, le conseil de la ville, qui avons ordonne que les veuves "
        "des bourgeois et les femmes qui sont dans la seigneurie pour le "
        "droit des enfants et pour la dot.</p>")
    root2 = _write(tmp_path / "fr", french)
    row2 = ingest_editio.parse(
        root2 / "data/ZH/ZH_TEST/SSRQ-ZH-TEST-1-1.xml", root2)

    assert row2["lang"] == "fr", "the header would have said 'de'"


def test_an_unclear_language_is_left_empty(tmp_path):
    """Roughly eight hundred transcriptions are Latin under a German editorial
    apparatus. A wrong tag drops them from a language filter in silence, so
    nothing is claimed."""
    root = _write(tmp_path, TEI.replace(
        "<p>Wir, der burgermeister und die raͤt der statt Zu̍rich, haben unns\n"
        "     vereinbart und bekenndt, das die froͧwen nach unnser statt recht\n"
        "     ußgericht werden soͤllen und nicht anders.</p>",
        "<p>Item.</p>"))
    row = ingest_editio.parse(
        root / "data/ZH/ZH_TEST/SSRQ-ZH-TEST-1-1.xml", root)

    assert row["lang"] == ""


def test_a_file_without_a_transcription_is_skipped(tmp_path):
    """Of 5,596 files, 968 are indices and front matter."""
    root = _write(tmp_path, TEI.replace("<text><body>", "<text><front>")
                                .replace("</body></text>", "</front></text>"))
    assert ingest_editio.parse(
        root / "data/ZH/ZH_TEST/SSRQ-ZH-TEST-1-1.xml", root) is None


def test_every_module_is_copied_into_the_image():
    """The Dockerfile lists modules individually rather than `COPY . .`.

    A new module is then easy to add to the repository and forget here, and
    nothing fails until the container starts: it builds cleanly, then
    crash-loops on ModuleNotFoundError. That is how hls_mcp shipped without
    embeddings.py and took the largest provider in the federation offline.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent
    # COPY lines only. Scanning the whole file would match module names in
    # comments — including the one above the COPY line, which made an earlier
    # version of this test pass with embeddings.py removed.
    copy_lines = [
        line for line in (root / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("COPY")
    ]
    copied = set(re.findall(r"([\w]+\.py)", " ".join(copy_lines)))
    shipped = {
        p.name for p in root.glob("*.py")
        if not p.name.startswith("test_")
        # Pipeline scripts run on the host, not in the serving image.
        and p.name not in {"ingest_editio.py", "embed_db.py", "conftest.py"}
    }
    assert not shipped - copied, f"not COPYed into the image: {sorted(shipped - copied)}"


def test_the_semantic_layer_has_every_name_it_uses():
    """The vector cache was left behind when these functions were brought over
    from kf_mcp, and nothing noticed until a query hit the server:
    NameError inside a tool surfaces to the caller as a bare
    "Error executing tool search_semantic".
    """
    import ast
    import pathlib

    tree = ast.parse((pathlib.Path(__file__).resolve().parent / "db.py")
                     .read_text(encoding="utf-8"))
    loaded = {n.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assigned |= {n.target.id for n in ast.walk(tree)
                 if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}

    missing = sorted(n for n in loaded
                     if n.startswith("_") and n.isupper() and n not in assigned)
    assert not missing, f"module-level names used but never defined: {missing}"


def test_every_sql_statement_matches_the_schema(tmp_path):
    """The semantic SQL still asked for Königsfelden's columns.

    short_id and source came across with the copied machinery and do not exist
    in this corpus. SQLite only complains when the statement runs, and inside
    an MCP tool that reaches the caller as a bare "Error executing tool
    search_semantic" — the cause stays in the container log.

    Executing each statement against an empty schema catches it at test time.
    """
    import sqlite3

    import db

    path = tmp_path / "schema.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript(db.EMBEDDING_SCHEMA_SQL)

    conn.execute(db._SEMANTIC_SQL.format(placeholders="?"), ("x",)).fetchall()
    conn.execute(db._DOC_FTS_SQL, ("wort", 1)).fetchall()
    conn.close()


def test_search_semantic_end_to_end(tmp_path):
    """Exercises the whole path, which the SQL-only test did not.

    Four separate failures reached the deployed server before this existed —
    a missing module in the image, a missing vector cache, Königsfelden's
    columns in the query, and a row key that did not match the alias. Each
    surfaced to the caller as the same opaque "Error executing tool
    search_semantic". Running the function over a real database with real
    vectors catches all four classes at once.
    """
    import sqlite3
    import struct

    import db

    path = tmp_path / "semantic.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript(db.EMBEDDING_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO documents (id, canton, volume, title, lang, origin_from, "
        "origin_to, place, text, n_chars, url) VALUES "
        "('SSRQ-ZH-T-1-1','ZH','ZH_T','Ordnung betreffend die Witwen','de',"
        "1446,1468,'Zürich','Wie froͧwen ußgericht werden soͤllen.',36,'http://x')")
    conn.execute(
        "INSERT INTO chunks (chunk_id, doc_id, chunk_index, char_start, char_end, text) "
        "VALUES ('SSRQ-ZH-T-1-1#0','SSRQ-ZH-T-1-1',0,0,36,"
        "'Wie froͧwen ußgericht werden soͤllen.')")
    # A unit vector, so the dot product is defined and the result deterministic.
    dims = 8
    vector = [1.0] + [0.0] * (dims - 1)
    conn.execute(
        "INSERT INTO embeddings (chunk_id, model, dims, vector) VALUES (?,?,?,?)",
        ("SSRQ-ZH-T-1-1#0", "test-model", dims,
         struct.pack(f"<{dims}f", *vector)))
    conn.commit(); conn.close()

    db.set_db_path(str(path))
    db._VECTOR_CACHE.clear()
    hits = db.search_semantic(vector, limit=5, model="test-model")

    assert len(hits) == 1
    hit = hits[0]
    assert hit["id"] == "SSRQ-ZH-T-1-1"
    # The year is the date of issue, aliased from origin_from.
    assert hit["year"] == 1446
    assert hit["canton"] == "ZH"
    assert hit["url"] == "http://x"
    assert hit["score"] > 0.99
