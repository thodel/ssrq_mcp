"""server.py — SSRQ MCP server (mcp 2.0 MCPServer, streamable HTTP)."""
import argparse, json, logging, os
from mcp.server.mcpserver import MCPServer
import db as db_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Defaults come from the environment so that importing this module never touches
# sys.argv — argparse at import time would hijack the arguments of any process that
# imports the server (tests, an ASGI loader). The CLI overrides these in main().
DEFAULT_DB   = os.environ.get("SSRQ_DB", "/data/ssrq.db")
DEFAULT_HOST = os.environ.get("SSRQ_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("SSRQ_PORT", "8002"))


def normalise_path(path):
    """A single leading slash, no trailing slash — the form the ASGI route wants.

    Behind a reverse proxy the server must answer on its *public* path: the
    streamable-HTTP transport builds no URLs of its own, but the route only
    matches what it was mounted at. Setting this to the public path (e.g.
    /mcp/ssrq/mcp) lets nginx proxy_pass without rewriting, which is the mismatch
    that makes a sub-path deployment 404."""
    cleaned = (path or "").strip().strip("/")
    return f"/{cleaned}" if cleaned else "/mcp"


DEFAULT_HTTP_PATH = normalise_path(os.environ.get("SSRQ_HTTP_PATH", "/mcp"))

MAX_YEAR_SPAN = 500

db_module.set_db_path(DEFAULT_DB)

mcp = MCPServer(
    name="SSRQ",
    version="1.0.0",
    instructions=(
        "The person and organisation authority file of the Sammlung Schweizerischer "
        "Rechtsquellen (SSRQ · SDS · FDS), the Collection of Swiss Law Sources "
        "published by the Rechtsquellenstiftung of the Swiss Law Society. "
        "The editions cover legal-historical documents from the Middle Ages to 1798. "
        "Persons use SSRQ identifiers (perXXXXXX), organisations orgXXXXXX. "
        "Use search_persons/search_orgs for name lookups, get_person/get_org for full "
        "records, search_name_index to reach historical spelling variants, and "
        "related_persons for family and institutional links."
    ),
)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def corpus_stats() -> dict:
    """High-level counts for the SSRQ authority file, plus the attested year range."""
    return db_module.stats()

@mcp.tool()
def list_persons(limit: int = 50, offset: int = 0) -> list[dict]:
    """Paginated list of the person authority file, ordered by SSRQ id."""
    return db_module.list_persons(limit, offset)

@mcp.tool()
def search_persons(query: str, limit: int = 50) -> list[dict]:
    """Search persons by name — standardised, label, or historical spelling variants."""
    return db_module.search_persons(query, limit)

@mcp.tool()
def get_person(pid: str) -> dict:
    """Full person record by SSRQ id (e.g. per000001), including its name variants."""
    result = db_module.get_person(pid)
    if not result:
        return {"error": f"Person '{pid}' not found."}
    return result

@mcp.tool()
def get_persons_by_year(year_from: int, year_to: int, limit: int = 100) -> list[dict]:
    """Persons whose attested years overlap a given range (inclusive)."""
    if year_to < year_from:
        return [{"error": "year_to must be >= year_from"}]
    if year_to - year_from > MAX_YEAR_SPAN:
        return [{"error": f"Year range too large; max {MAX_YEAR_SPAN} years."}]
    return db_module.get_persons_by_year(year_from, year_to, limit)

@mcp.tool()
def search_orgs(query: str, limit: int = 50) -> list[dict]:
    """Search the organisation authority file by name."""
    return db_module.search_orgs(query, limit)

@mcp.tool()
def get_org(oid: str) -> dict:
    """Full organisation record by SSRQ id (e.g. org000001), including its name variants."""
    result = db_module.get_org(oid)
    if not result:
        return {"error": f"Organisation '{oid}' not found."}
    return result

@mcp.tool()
def search_name_index(query: str, type_filter: str = "", limit: int = 50) -> list[dict]:
    """Search every recorded name variant. Set type_filter to 'person' or 'org' to
    restrict; each row reports its own `kind`, the matched `name_text`, and the
    canonical label of the entity it belongs to."""
    return db_module.search_name_index(query, type_filter or None, limit)

@mcp.tool()
def get_name_variants(id: str) -> list[dict]:
    """All name variants on record for a given person or organisation id."""
    return db_module.get_name_variants(id)

@mcp.tool()
def related_persons(pid: str) -> dict:
    """A person's spouses, parents, organisations, and places, resolved to records."""
    return db_module.related_persons(pid)

# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("ssrq://stats")
def resource_stats() -> str:
    return json.dumps(db_module.stats(), indent=2)

@mcp.resource("ssrq://orgs")
def resource_orgs() -> str:
    """Brief organisation index: id, label, std_name, type. Flags its own truncation."""
    return json.dumps(db_module.org_index(), indent=2, ensure_ascii=False)

@mcp.resource("ssrq://person/{pid}")
def resource_person(pid: str) -> str:
    result = db_module.get_person(pid)
    if not result:
        return json.dumps({"error": f"Person '{pid}' not found."})
    return json.dumps(result, indent=2, ensure_ascii=False)

@mcp.resource("ssrq://org/{oid}")
def resource_org(oid: str) -> str:
    result = db_module.get_org(oid)
    if not result:
        return json.dumps({"error": f"Organisation '{oid}' not found."})
    return json.dumps(result, indent=2, ensure_ascii=False)

# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="SSRQ MCP server")
    ap.add_argument("--db",   default=DEFAULT_DB,   help="Path to ssrq.db (env SSRQ_DB)")
    ap.add_argument("--host", default=DEFAULT_HOST, help="Bind address (env SSRQ_HOST)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (env SSRQ_PORT)")
    ap.add_argument("--http-path", default=DEFAULT_HTTP_PATH,
                    help="Path the MCP endpoint is served at; set it to the public "
                         "path when behind a reverse proxy (env SSRQ_HTTP_PATH)")
    args = ap.parse_args(argv)
    args.http_path = normalise_path(args.http_path)
    return args

def main(argv=None):
    args = parse_args(argv)
    db_module.set_db_path(args.db)

    logger.info(f"Database: {args.db}")
    try:
        s = db_module.stats()
        logger.info(f"Corpus: {s['n_persons']:,} persons, {s['n_orgs']:,} orgs, "
                    f"{s['n_name_index']:,} name variants")
    except Exception as e:
        logger.warning(f"Could not read DB stats: {e}")
    logger.info(f"Starting SSRQ MCP server on {args.host}:{args.port}{args.http_path}")
    mcp.run(transport="streamable-http", host=args.host, port=args.port,
            streamable_http_path=args.http_path)

if __name__ == "__main__":
    main()
