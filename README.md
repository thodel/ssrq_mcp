# SSRQ — MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes the person and organisation
authority file of the **Sammlung Schweizerischer Rechtsquellen** — *Les sources du droit
suisse* / *Le fonti del diritto svizzero*, in English the **Collection of Swiss Law
Sources** (SSRQ · SDS · FDS) — to Claude and other MCP-compatible clients.

The collection is published by the Rechtsquellenstiftung of the Swiss Law Society and
comprises over 140 editions of legal-historical documents from the Middle Ages to 1798
(<https://ssrq-sds-fds.ch>). This server serves the authority file behind those editions:
23,674 persons, 7,047 organisations, and 138,298 name variants.

## Architecture

```
ssrq__fuseki_*.ttl  ──►  SSRQ ETL  ──►  ssrq.db (SQLite)
                                          persons ────┐
                                          orgs ───────┼──►  server.py
                                          name_index ─┘     (mcp 2.0 MCPServer,
                                                             streamable HTTP)
                                                                   │
                                                         http://<host>:8002/mcp
```

The server targets **mcp 2.0**, which renamed the high-level server class
(`FastMCP` → `MCPServer`) and removed `mcp.server.fastmcp`; `requirements.txt` pins the
major version accordingly.

The database is built by the SSRQ project's ETL pipeline from the RDF-TTL source dump;
this repository only serves it. Every connection is opened `mode=ro` with
`PRAGMA query_only`, so the server cannot write to the corpus.

## Setup

### 1. Build the database

The database lives at `/data/ssrq.db` in the container. To rebuild it from the RDF-TTL
source (in the SSRQ project repository):

```bash
python ssrq_parse_ttl.py --input /path/to/ssrq__fuseki_*.ttl --db ssrq.db
```

`db.SCHEMA_SQL` holds the schema this server expects — it is the contract between the
ETL and the server, and the tests build their fixtures from it.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the server

```bash
python server.py --db ssrq.db --host 0.0.0.0 --port 8002
```

Each flag also has an environment variable — `SSRQ_DB`, `SSRQ_HOST`, `SSRQ_PORT`,
`SSRQ_HTTP_PATH` — which the flags override. Importing `server.py` never reads
`sys.argv`, so it is safe to import from tests or an ASGI loader.

`--http-path` (default `/mcp`) is the path the MCP endpoint is served at. **Behind a
reverse proxy, set it to the public path** — see [Reverse proxy](#reverse-proxy-nginx).

### 4. Connect a client

Add to your `claude_desktop_config.json` (or equivalent):

```json
{
  "mcpServers": {
    "ssrq": {
      "url": "http://<server-ip>:8002/mcp",
      "transport": "streamable-http"
    }
  }
}
```

Or for Claude Code:

```bash
claude mcp add ssrq --transport http --url http://<server-ip>:8002/mcp
```

---

## Docker deployment

### Build image

```bash
docker compose build
```

### Run

```bash
docker compose up -d
```

The container serves on port 8002 and expects `ssrq.db` at `/data/ssrq.db`. Adjust the
volume path in `docker-compose.yml` if your data lives elsewhere.

### Reverse proxy (nginx)

<a id="reverse-proxy-nginx"></a>

Serving under a sub-path (`https://tei.example.ch/mcp/ssrq/mcp`) has exactly one rule:
**the app's `--http-path` and the nginx `location` must be the same string.** The
endpoint is one path that answers `POST` (requests), `GET` (the server→client stream),
and `DELETE` (session teardown); it builds no URLs of its own, so all nginx has to do is
forward the path unchanged.

```nginx
server {
    listen 443 ssl;
    server_name tei.example.ch;

    # SSRQ_HTTP_PATH=/mcp/ssrq/mcp — same string, no trailing slash on proxy_pass,
    # so the path reaches the app unrewritten.
    location /mcp/ssrq/mcp {
        proxy_pass         http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        # The GET stream must not be buffered or timed out mid-session.
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }
}
```

Two failure modes worth knowing, both of which return a bare `Not Found` or `405`:

- **A trailing slash on `proxy_pass`** (`http://127.0.0.1:8002/`) strips the location
  prefix, so the app sees `/` and no route matches.
- **`location` and `--http-path` disagree** — the app 404s every request. Check the
  startup line, which prints the exact path being served:
  `Starting SSRQ MCP server on 0.0.0.0:8002/mcp/ssrq/mcp`.

Verify from outside before wiring up a client:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://tei.example.ch/mcp/ssrq/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

`200` means the endpoint is live. `404` is a path mismatch, `405` means nginx is not
passing `POST` to the app (a static `location` or a `limit_except` is shadowing it).

> **Note:** the server has no authentication. By default `docker-compose.yml` publishes
> port 8002 on all interfaces; if a proxy fronts it, bind it to loopback instead so the
> authority file is not reachable directly:
>
> ```bash
> SSRQ_BIND=127.0.0.1 docker compose up -d
> ```
>
> Otherwise restrict access at the firewall.

---

## Available tools

| Tool | Description |
|------|-------------|
| `corpus_stats()` | Person/org/name-variant counts and the attested year range |
| `list_persons(limit=50, offset=0)` | Paginated list of the person authority file, by id |
| `search_persons(query, limit=50)` | Persons by standardised name, label, or spelling variant |
| `get_person(pid)` | Full person record by SSRQ id (e.g. `per000001`), with name variants |
| `get_persons_by_year(year_from, year_to, limit=100)` | Persons whose attested years overlap a range (max span 500 years) |
| `search_orgs(query, limit=50)` | Organisation authority by name |
| `get_org(oid)` | Full org record by SSRQ id (e.g. `org000001`), with name variants |
| `search_name_index(query, type_filter="", limit=50)` | Search all 138k name variants; `type_filter` is `person`, `org`, or empty for both |
| `get_name_variants(id)` | All name variants for a given person or org id |
| `related_persons(pid)` | Spouses, mothers, fathers, organisations, and places, resolved to records |

## Available resources

| URI | Description |
|-----|-------------|
| `ssrq://stats` | Corpus statistics (JSON) |
| `ssrq://orgs` | Organisation index — `{total, returned, truncated, orgs: [...]}`, capped at 1000 rows and flagged when truncated |
| `ssrq://person/{pid}` | Single person record (JSON) |
| `ssrq://org/{oid}` | Single organisation record (JSON) |

## Query behaviour

**Limits.** Every `limit` is clamped to at most 500; a negative, zero, or non-numeric
value falls back to that tool's own default rather than returning the whole table. Use
`list_persons(limit, offset)` to page through the register.

**Result size.** Claude.ai and Claude Desktop truncate a tool or resource result at
roughly 150,000 characters. `ssrq://orgs` is capped at 1000 rows (about 100 KB) for that
reason and reports its own truncation; the 500-row tool ceiling stays comfortably under
the limit too.

**Name search.** `search_persons`, `search_orgs`, and `search_name_index` do a plain
case-insensitive substring match. SQL wildcards in the query are escaped, so searching for
`100%` finds a literal "100%" rather than matching every record. `search_persons` looks at
`std_name`, `label`, and both spelling-variant columns; historical spellings that differ
from the modern form are best reached through `search_name_index`.

**Name index shape.** Every row carries `kind` (`person` or `org`), so the result shape is
the same whether or not `type_filter` is set.

**Missing records.** `get_person`, `get_org`, and `related_persons` return
`{"error": "... not found."}` rather than raising.

**Year ranges.** `get_persons_by_year` matches on *overlap*: a person is returned when
`first_year <= year_to` and `last_year >= year_from`. Persons with no attested years are
never returned. An inverted range, or one spanning more than 500 years, comes back as an
error object.

**Places.** `related_persons` resolves `spouse_ids`, `mother_ids`, `father_ids`, and
`org_ids` against the `persons` and `orgs` tables. `loc_ids` point at the SSRQ place
authority, which this database does not currently carry: those ids are returned as bare
`{"id": ...}` entries together with a `places_note`. If a `places` (or `locations`) table
is added to the database later, they are resolved to full records automatically.

## Database schema

| Table | Contents |
|-------|----------|
| `persons` | id, uri, etype, label, label_lang, std_name, forename, surname, sex, first_year, last_year, years, org_ids, spouse_ids, mother_ids, father_ids, loc_ids, orig_names, std_names |
| `orgs` | id, uri, etype, label, std_name, surname, alias_of, org_type |
| `name_index` | name_text, ssrq_id, is_orig (138k variant → canonical mappings) |

### Key notes

- **Person IDs:** `per000001`–`per999999` (23,674 total)
- **Org IDs:** `org000001`–`org999999` (7,047 total)
- `orig_names` / `std_names` — original and normalised spelling variants (comma-joined)
- `is_orig=1` in `name_index` means the name is the original spelling; `is_orig=0` is a
  normalised variant. Original spellings sort first in every variant listing.
- The relation columns (`org_ids`, `spouse_ids`, `mother_ids`, `father_ids`, `loc_ids`)
  are comma-joined id lists; `related_persons` resolves them all in one call.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest test_ssrq_mcp.py
```

Unit tests build their own throwaway database and run with no setup. The DB and server
tests skip unless you point them at the real database and a running server:

```bash
SSRQ_DB=/data/ssrq.db SSRQ_SERVER=http://localhost:8002 pytest test_ssrq_mcp.py
```

The suite also runs standalone, with grouped output and a non-zero exit on failure:

```bash
python test_ssrq_mcp.py --unit --db /data/ssrq.db --server http://localhost:8002
```

Note that the DB tests assert corpus-size floors (≥20,000 persons, ≥6,000 organisations,
≥100,000 name variants) — they will fail against a small sample database.
