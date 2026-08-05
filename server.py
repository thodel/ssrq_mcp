"""server.py — SSRQ MCP server (mcp 2.0, callback-based Server API)."""
import json
import logging
import os
from typing import Any

from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
    ListToolsResult,
)

import db as db_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Tool registry ─────────────────────────────────────────────────────────────

TOOLS: list[Tool] = [
    Tool(
        name="corpus_stats",
        title="Corpus Statistics",
        description="High-level row counts for the SSRQ corpus.",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="search_persons",
        title="Search Persons",
        description="Search the person authority by name. Returns id, label, std_name, life dates.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name fragment to search for."},
                "limit": {"type": "integer", "default": 50, "description": "Max results."},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_person",
        title="Get Person",
        description="Full person record by SSRQ id (e.g. per000001).",
        input_schema={
            "type": "object",
            "properties": {
                "pid": {"type": "string", "description": "SSRQ person id (e.g. per000001)."},
            },
            "required": ["pid"],
        },
    ),
    Tool(
        name="search_orgs",
        title="Search Organisations",
        description="Search the organisation authority by name.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Organisation name fragment."},
                "limit": {"type": "integer", "default": 50, "description": "Max results."},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_org",
        title="Get Organisation",
        description="Full organisation record by SSRQ id (e.g. org000001).",
        input_schema={
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "SSRQ org id (e.g. org000001)."},
            },
            "required": ["oid"],
        },
    ),
    Tool(
        name="search_name_index",
        title="Search Name Index",
        description="Search all name variants (138k entries) by name fragment. Returns all matching persons and orgs with their canonical label.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name fragment to search."},
                "type_filter": {"type": "string", "enum": ["person", "org"], "description": "Restrict to persons or orgs."},
                "limit": {"type": "integer", "default": 50, "description": "Max results."},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_name_variants",
        title="Name Variants",
        description="All name variants on record for a given person or org id.",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "SSRQ person or org id."},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="related_persons",
        title="Related Persons",
        description="Person's related persons, orgs, and places via spouse/family/org membership links.",
        input_schema={
            "type": "object",
            "properties": {
                "pid": {"type": "string", "description": "SSRQ person id."},
            },
            "required": ["pid"],
        },
    ),
]


def _call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    """Dispatch to the appropriate dbModule function and wrap the result."""
    if name == "corpus_stats":
        data = db_module.stats()
    elif name == "search_persons":
        data = db_module.search_persons(arguments["query"], arguments.get("limit", 50))
    elif name == "get_person":
        result = db_module.get_person(arguments["pid"])
        data = result if result else {"error": f"Person '{arguments['pid']}' not found."}
    elif name == "search_orgs":
        data = db_module.search_orgs(arguments["query"], arguments.get("limit", 50))
    elif name == "get_org":
        result = db_module.get_org(arguments["oid"])
        data = result if result else {"error": f"Organisation '{arguments['oid']}' not found."}
    elif name == "search_name_index":
        data = db_module.search_name_index(
            arguments["query"],
            arguments.get("type_filter"),
            arguments.get("limit", 50),
        )
    elif name == "get_name_variants":
        data = db_module.get_name_variants(arguments["id"])
    elif name == "related_persons":
        data = db_module.related_persons(arguments["pid"])
    else:
        data = {"error": f"Unknown tool: {name}"}

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False))]
    )


# ── Server setup ──────────────────────────────────────────────────────────────

INSTRUCTIONS = (
    "The Swiss Summary of Roman Law (SSRQ) person and organisation authority file. "
    "Covers legal professionals, institutions, and related entities from Roman law sources. "
    "Person IDs: perXXXXXX; Organisation IDs: orgXXXXXX. "
    "Use search_persons/search_orgs for name lookups; "
    "use get_person/get_org for full records; "
    "use search_name_index to search all 138k name variants."
)


def make_server() -> Server:
    """Build an SSRQ MCP server instance."""

    async def list_tools() -> ListToolsResult:
        return ListToolsResult(tools=TOOLS)

    async def call_tool(ctx, params) -> CallToolResult:
        name = params.name
        arguments = dict(params.arguments) if params.arguments else {}
        return _call_tool(name, arguments)

    server = Server(
        name="SSRQ",
        version="1.0.0",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )

    return server


# ── CLI entry point ───────────────────────────────────────────────────────────

def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="SSRQ MCP server")
    ap.add_argument("--db",   default=os.environ.get("SSRQ_DB",   "/data/ssrq.db"))
    ap.add_argument("--host", default=os.environ.get("SSRQ_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SSRQ_PORT", "8002")))
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    db_module.set_db_path(args.db)

    logger.info(f"Database: {args.db}")
    try:
        s = db_module.stats()
        logger.info(
            f"Corpus: {s['persons']:,} persons, {s['orgs']:,} orgs, "
            f"{s['name_index']:,} name variants"
        )
    except Exception as e:
        logger.warning(f"Could not read DB stats: {e}")

    server = make_server()

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        host=args.host,
        json_response=False,
    )

    import uvicorn
    logger.info(f"Starting SSRQ MCP server on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
