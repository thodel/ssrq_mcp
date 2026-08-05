"""Tests for the SSRQ MCP server.

Run standalone:
    python test_ssrq_mcp.py --unit
    python test_ssrq_mcp.py --db /path/to/ssrq.db --server http://localhost:8002

Run via pytest:
    pytest test_ssrq_mcp.py
"""
from __future__ import annotations

import argparse
import sys
import os

# ── helpers ───────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("SSRQ_DB")
SERVER_URL = os.environ.get("SSRQ_SERVER")


def http_post(payload: dict) -> dict:
    import urllib.request
    data = __import__("json").dumps(payload).encode()
    req = urllib.request.Request(
        SERVER_URL,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return __import__("json").loads(resp.read())


def ping_server() -> bool:
    """Return True if the server is reachable."""
    try:
        import urllib.request
        urllib.request.urlopen(f"{SERVER_URL}/", timeout=3)
        return True
    except Exception:
        return False


# ── unit tests ────────────────────────────────────────────────────────────────

def test_import_server():
    import db as db_module
    import server as server_module
    assert hasattr(db_module, "stats")
    assert hasattr(server_module, "make_server")
    print("  import_server ✓")


def test_db_stats():
    import db as db_module
    s = db_module.stats()
    assert isinstance(s, dict)
    assert "persons" in s and "orgs" in s and "name_index" in s
    assert s["persons"] >= 20000, f"Expected >=20k persons, got {s['persons']}"
    assert s["orgs"] >= 6000, f"Expected >=6k orgs, got {s['orgs']}"
    assert s["name_index"] >= 100000, f"Expected >=100k name variants, got {s['name_index']}"
    print(f"  db_stats ✓  (persons={s['persons']:,}, orgs={s['orgs']:,}, names={s['name_index']:,})")


def test_db_get_person():
    import db as db_module
    p = db_module.get_person("per000001")
    assert p is not None, "per000001 should exist"
    assert p["id"] == "per000001"
    assert "std_name" in p
    assert "orig_names" in p
    not_found = db_module.get_person("per999999")
    assert not_found is None, "non-existent person should return None"
    print(f"  db_get_person ✓  (std_name={p.get('std_name', '?')!r})")


def test_db_get_org():
    import db as db_module
    o = db_module.get_org("org000001")
    assert o is not None, "org000001 should exist"
    assert o["id"] == "org000001"
    print(f"  db_get_org ✓  (label={o.get('label', '?')!r})")


def test_db_search_persons():
    import db as db_module
    results = db_module.search_persons("Heinrich", limit=5)
    assert isinstance(results, list)
    if results:
        assert "per" in results[0]["id"]
    print(f"  db_search_persons ✓  ({len(results)} results for 'Heinrich')")


def test_db_search_orgs():
    import db as db_module
    results = db_module.search_orgs("Stadt", limit=5)
    assert isinstance(results, list)
    print(f"  db_search_orgs ✓  ({len(results)} results for 'Stadt')")


def test_db_search_name_index():
    import db as db_module
    results = db_module.search_name_index("Johann", None, limit=10)
    assert isinstance(results, list)
    assert all("ssrq_id" in r for r in results)
    print(f"  db_search_name_index ✓  ({len(results)} results for 'Johann')")

    # Filter to persons only
    p_results = db_module.search_name_index("Brug", "person", limit=5)
    assert all("per" in r["ssrq_id"] for r in p_results)
    print(f"  db_search_name_index(person) ✓  ({len(p_results)} results for 'Brug/person')")

    # Filter to orgs only
    o_results = db_module.search_name_index("Stadt", "org", limit=5)
    assert all("org" in r["ssrq_id"] for r in o_results)
    print(f"  db_search_name_index(org) ✓  ({len(o_results)} results for 'Stadt/org')")


def test_db_get_name_variants():
    import db as db_module
    variants = db_module.get_name_variants("per000001")
    assert isinstance(variants, list)
    print(f"  db_get_name_variants ✓  ({len(variants)} variants for per000001)")


def test_db_related_persons():
    import db as db_module
    # Find a person with spouses or parents first
    import sqlite3
    con = sqlite3.connect(f"file:{db_module.DB_PATH}?mode=ro", uri=True)
    row = con.execute(
        "SELECT id FROM persons WHERE spouse_ids IS NOT NULL AND spouse_ids != '' LIMIT 1"
    ).fetchone()
    con.close()
    if row:
        pid = row[0]
        related = db_module.related_persons(pid)
        assert "person" in related
        print(f"  db_related_persons ✓  (spouse_ids={related['person'].get('spouse_ids','')!r})")
    else:
        print("  db_related_persons ⊘  (no person with spouse_ids found, skipping)")


def run_unit_tests():
    print("\n── Unit tests (no DB/server required) ──")
    test_import_server()
    print()
    print("── DB tests (require SSRQ_DB env var) ──")
    if not DB_PATH:
        print("  ⊘  SSRQ_DB not set; skipping DB tests")
    else:
        os.environ["SSRQ_DB"] = DB_PATH  # make sure db module picks it up
        import importlib, db as db_module
        db_module.set_db_path(DB_PATH)
        importlib.reload(db_module)
        test_db_stats()
        test_db_get_person()
        test_db_get_org()
        test_db_search_persons()
        test_db_search_orgs()
        test_db_search_name_index()
        test_db_get_name_variants()
        test_db_related_persons()


# ── server integration tests ──────────────────────────────────────────────────

def test_server_initialize():
    result = http_post({
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 0,
    })
    assert "result" in result, f"Expected result, got: {result}"
    assert result["result"]["serverInfo"]["name"] == "SSRQ"
    print(f"  server_initialize ✓  (server: {result['result']['serverInfo']['name']} v{result['result']['serverInfo']['version']})")


def test_server_tools_list():
    result = http_post({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "params": {},
        "id": 1,
    })
    assert "result" in result, f"Expected result, got: {result}"
    tool_names = [t["name"] for t in result["result"]["tools"]]
    expected = ["corpus_stats", "search_persons", "get_person", "search_orgs",
                "get_org", "search_name_index", "get_name_variants", "related_persons"]
    for name in expected:
        assert name in tool_names, f"Tool {name!r} not in {tool_names}"
    print(f"  server_tools_list ✓  ({len(tool_names)} tools)")


def test_server_call_corpus_stats():
    result = http_post({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "corpus_stats", "arguments": {}},
        "id": 2,
    })
    assert "result" in result, f"Expected result, got: {result}"
    content = result["result"]["content"][0]["text"]
    import json
    stats = json.loads(content)
    assert "persons" in stats and "orgs" in stats
    print(f"  server_call_corpus_stats ✓  (persons={stats['persons']:,}, orgs={stats['orgs']:,})")


def run_server_tests():
    print("\n── Server tests (require SSRQ_SERVER env var and running server) ──")
    if not SERVER_URL:
        print("  ⊘  SSRQ_SERVER not set; skipping server tests")
        return
    if not ping_server():
        print("  ⊘  Server not reachable; skipping server tests")
        return
    test_server_initialize()
    test_server_tools_list()
    test_server_call_corpus_stats()


# ── CLI runner ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", action="store_true", help="Run unit tests only")
    ap.add_argument("--db", default=os.environ.get("SSRQ_DB"), help="Path to ssrq.db")
    ap.add_argument("--server", default=os.environ.get("SSRQ_SERVER"), help="Server URL e.g. http://localhost:8002")
    args = ap.parse_args()

    global DB_PATH, SERVER_URL
    DB_PATH = args.db
    SERVER_URL = args.server

    if args.unit:
        run_unit_tests()
    else:
        run_unit_tests()
        run_server_tests()

    print()


if __name__ == "__main__":
    main()
