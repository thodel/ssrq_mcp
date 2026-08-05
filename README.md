# SSRQ — MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes the **Swiss Summary of
Roman Law (SSRQ)** person and organisation authority file — 23,674 persons, 7,047
organisations, and 138,298 name variants — to Claude and other MCP-compatible clients.

## Architecture

```
ssrq.db (SQLite, read-only)           server.py
  persons ─────────────────────────────►  Tool registry
  orgs    ─────────────────────────────►  (mcp 2.0 Server API)
  name_index ──────────────────────────►  streamable-http transport
                                              │
                                     http://<host>:8002/mcp
```

The database is built by the SSRQ project's ETL pipeline from the RDF-TTL source dump
(`ssrq__fuseki_*.ttl`). This server is read-only (`PRAGMA query_only`).

## Setup

### 1. Build the database

The database lives at `/data/ssrq.db` in the container. If you need to rebuild from
the RDF-TTL source:

```bash
python ssrq_parse_ttl.py --input /path/to/ssrq__fuseki_*.ttl --db ssrq.db
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the server

```bash
python server.py --db ssrq.db --host 0.0.0.0 --port 8002
```

Each flag also has an environment variable — `SSRQ_DB`, `SSRQ_HOST`, `SSRQ_PORT` —
which the flags override.

### 4. Connect a client

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

The container serves on port 8002 and expects `ssrq.db` at `/data/ssrq.db`.

### Reverse proxy (nginx, optional but recommended)

```nginx
server {
    listen 443 ssl;
    server_name ssrq-mcp.example.unibe.ch;

    location / {
        proxy_pass         http://localhost:8002;
        proxy_http_version 1.1;
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        chunked_transfer_encoding on;
    }
}
```

---

## Available tools

| Tool | Description |
|------|-------------|
| `corpus_stats()` | Row counts — persons, orgs, name variants |
| `search_persons(query, limit=50)` | Person authority by name (std_name, label, or orig_names) |
| `get_person(pid)` | Full person record by SSRQ id (e.g. `per000001`) |
| `search_orgs(query, limit=50)` | Organisation authority by name |
| `get_org(oid)` | Full org record by SSRQ id (e.g. `org000001`) |
| `search_name_index(query, type_filter, limit=50)` | Search all 138k name variants; optionally filter to `person` or `org` |
| `get_name_variants(id)` | All name variants for a given person or org id |
| `related_persons(pid)` | Person's spouses, mothers, fathers, org memberships, places |

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
- `is_orig=1` in `name_index` means the name is the original spelling; `is_orig=0` is a normalised variant
- `related_persons` resolves all comma-joined IDs in spouse_ids / mother_ids / father_ids / org_ids / loc_ids in one call
