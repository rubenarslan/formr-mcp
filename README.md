# formr-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for the [formr](https://formr.org) survey framework. Let LLM agents inspect, create, and edit formr run structures, manage settings, and get design guidance — all through natural language.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **formr instance** with admin (≥ 2) API access — create credentials at `<base_url>/admin/account#api`
- **R** (optional) — enables R syntax validation in `analyze_run`

## Quick Start

```bash
git clone https://github.com/timseidel/formr-mcp && cd formr-mcp
cp .env.example .env
# Edit .env with your formr instance URL and API credentials
uv sync
uv run server.py
```

### Environment Variables

| Variable | Description |
|---|---|
| `FORMR_BASE_URL` | formr instance URL (e.g. `http://localhost`) |
| `FORMR_CLIENT_ID` | 32-char hex client ID from `/admin/account#api` |
| `FORMR_CLIENT_SECRET` | 64-char hex secret from `/admin/account#api` |
| `FORMR_DATA_ACCESS` | (optional) participant-data ceiling: `test_only` (default) or `all` |
| `FORMR_HTTP_TIMEOUT` | (optional) HTTP timeout in seconds (default `60`) |
| `FORMR_MCP_LOG_LEVEL` | (optional) log verbosity (default `INFO`; use `DEBUG` for raw httpx lines) |
| `FLOWCHART_URL` | (optional) URL of the formr Flowchart Generator (default: `https://formr-flowchart-test.pages.dev`) |

## MCP Client Configuration

### opencode

Add to `opencode.json` in your project root:

```json
{
  "mcp": {
    "formr-mcp": {
      "type": "local",
      "command": ["uv", "run", "server.py"],
      "enabled": true
    }
  }
}
```

Or copy from the included example:

```bash
cp opencode.json.example opencode.json
```

### Claude Desktop

Add to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "formr-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/formr-mcp", "server.py"],
      "env": {
        "FORMR_BASE_URL": "http://localhost",
        "FORMR_CLIENT_ID": "your_client_id",
        "FORMR_CLIENT_SECRET": "your_client_secret"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "formr-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/formr-mcp", "server.py"]
    }
  }
}
```

Set `FORMR_BASE_URL`, `FORMR_CLIENT_ID`, and `FORMR_CLIENT_SECRET` in a `.env` file in the project root.

## Hosted server: add formr to Claude by its URL

A formr instance can run this server for all its researchers
(`http_server.py`; formr-docker's `docker-compose-mcp.yml` layer). Nothing is
configured per user:

- **claude.ai / Claude Desktop:** Settings → Connectors → Add custom connector,
  URL `https://<formr host>/mcp`.
- **Claude Code:** `claude mcp add --transport http formr https://<formr host>/mcp`,
  then `/mcp` to sign in.

The client is sent to formr to sign in (login, 2FA, admin level 2) and to
choose the connection's scopes and runs, as for an API credential. formr is
the OAuth server: it identifies Claude by its client ID metadata document,
requires PKCE, and lists each connection under Account → API, where the
researcher changes or deletes it (deleting ends it at once). Every request
carries that connection's token; the server checks it with formr
(`GET /api/v1/user/token`) and calls the API with it, so formr's scopes, run
allowlist and run ownership decide every call. The server keeps no
credential, secret or file: the workspace tools fetch the run from formr and
upload an edit in one step, and `get_run_structure_to_file`,
`update_run_structure_from_file` and `open_flowchart` are not offered.
`FORMR_DATA_ACCESS` (default `test_only`) is the operator's ceiling for every
user. Environment: `FORMR_BASE_URL` (formr's admin origin, the issuer),
`MCP_PUBLIC_URL` (this server's URL, formr's `FORMR_MCP_URL`). Needs formr
v2.0.0-rc13 or later.

## Available Tools

### Run Management

| Tool | Description |
|---|---|
| `list_runs` | List all runs, optionally filter by name |
| `get_run` | Get run metadata by exact name |
| `create_run` | Create a new empty run |
| `delete_run` | Permanently delete a run and all its data |
| `whoami` | Identify the user **and report this token's capabilities** — granted scopes, read-only/run-allowlist limits, data-access mode, and a per-tool availability map. Call this first. |

### Reading Data

| Tool | Description |
|---|---|
| `list_sessions` | List participant sessions (code, position, ended, testing flag) |
| `get_session` | Get one session by its code |
| `list_unit_sessions` | Per-unit history for progress / dropout / trajectory analysis |
| `get_run_results` | Raw survey responses, keyed by survey (filter by survey/session/item) |
| `list_run_files` | List uploaded-file metadata |
| `get_survey_structure` | Survey item definitions and choice lists (not participant data) |

> **Data-access gate (GDPR).** `FORMR_DATA_ACCESS` sets a hard ceiling on what the read
> tools may return: `test_only` (the default) exposes only test sessions (`testing=1`);
> `all` exposes real participant data. Per-call test/real filters apply within the ceiling
> but can never exceed it. See `.env.example` and the `data-access` documentation topic.

### Structure Editing

| Tool | Description |
|---|---|
| `get_run_structure_to_file` | Fetch full structure to `.formr/<name>.json` (backs up existing) |
| `update_run_structure_from_file` | Validate and upload from `.formr/<name>.json` |
| `get_run_structure` | Fetch full structure as **JSON text** (no file — for clients without filesystem access, e.g. Claude Desktop) |
| `update_run_structure` | Validate and upload a structure passed as **JSON text** |
| `update_run_settings` | Change run-level settings (title, visibility, etc.) |

### Inspection & Analysis

| Tool | Description |
|---|---|
| `summarize_run` | Human-readable overview (`detail="units"` or `"items"`) |
| `find_run_items` | Search items by name/label substring and/or type |
| `analyze_run` | Check R syntax, variable refs, branch flow, item consistency |
| `open_flowchart` | Upload run structure and get a shareable flowchart URL (expires in 24h) |

### Documentation

| Tool | Description |
|---|---|
| `get_documentation` | Learn formr design (topics: `item-types`, `run-concepts`, `r-code`, `survey-json`, `examples`, `best-practices`, `form-v2`) |
| `get_unit_types` | Get all supported unit types and their required/optional fields |
| `get_documentation_topics` | List available documentation topics |

### Utilities

| Tool | Description |
|---|---|
| `fetch_google_sheet` | Fetch a Google Sheet by its share/edit link as **CSV** (returned as text, or `to_file=True` to write `.formr/sheets/*.csv`) or **XLSX** (written to a file). Canonicalizes the link into the export URL for you. |

## Importing from Google Sheets

formr surveys are often authored in Google Sheets. A client like Claude Desktop
can't turn a sheet's share link into the CSV/XLSX export URL on its own, so use
`fetch_google_sheet`:

```
# read a tab as CSV text (default) — no filesystem connector needed
fetch_google_sheet("https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0")

# write the workbook as XLSX (e.g. to upload via formr's survey import)
fetch_google_sheet("<sheet link>", format="xlsx")
```

The sheet must be link-accessible (**Share → Anyone with the link → Viewer**) or
published to the web; a private sheet returns a clear sharing error. The `gid` in
the link selects the tab for CSV; XLSX always exports the whole workbook.

## Structure Editing Workflow

Run structures can be large. **With filesystem access** (Claude Code, editors), prefer the
**file-based workflow** instead of passing JSON through tool arguments:

```
1. Fetch   get_run_structure_to_file("my-run")
           → writes .formr/my-run.json

2. Edit    Use your editor/agent to modify .formr/my-run.json

3. Upload  update_run_structure_from_file("my-run")
           → validates, uploads, removes backup on success
```

- If `.formr/my-run.json` already exists, the previous version is backed up to `.formr/my-run.json.bak`
- On upload validation errors, fix the file and retry — restore from `.bak` if stuck
- The `.formr/` directory is gitignored

**Without filesystem access** (e.g. Claude Desktop with no filesystem connector), use the
inline text tools instead — same validation, no files:

```
1. Fetch   get_run_structure("my-run")          → returns the structure as JSON text
2. Edit    Modify the JSON inline
3. Upload  update_run_structure("my-run", "<json text>")  → validates and uploads
```

## Project Structure

```
formr-mcp/
├── server.py                  # MCP server entry point, tool definitions
├── formr_mcp/
│   ├── auth.py                # OAuth2 client credentials flow
│   ├── client.py              # HTTP client for formr REST API
│   ├── documentation.py       # Built-in design documentation topics
│   ├── analysis.py            # Run structure analysis (R syntax, refs, flow)
│   ├── summarize.py           # Run summarization and item search
│   └── validation.py          # Structure validation (types, positions, choices)
├── tests/
│   ├── test_server.py         # Server and tool tests
│   ├── test_analysis.py       # Analysis module tests
│   ├── test_summarize.py       # Summarization tests
│   └── test_normalize_choices.py # Choice normalization tests
├── .env.example               # Environment variable template
├── opencode.json.example      # opencode MCP config template
└── pyproject.toml             # Project metadata and dependencies
```

## Logs

The server logs to **stderr** (stdout is the JSON-RPC channel and must stay
clean). At the default `INFO` level it logs each tool call (name, arg preview,
duration, success/error) and each formr API request (status + duration);
`FORMR_MCP_LOG_LEVEL=DEBUG` additionally surfaces raw httpx lines.

Where stderr ends up depends on the client:

- **Claude Desktop** captures it per server:
  `~/Library/Logs/Claude/mcp-server-<name>.log` (macOS),
  `%APPDATA%\Claude\logs\mcp-server-<name>.log` (Windows). `<name>` is your
  config key. `tail -f` it.
- **Claude Code** — run `claude --debug` to see it inline; `/mcp` shows status.
- **See it live / debug directly** — run the server yourself with the MCP
  Inspector: `npx @modelcontextprotocol/inspector uv run server.py`, or run the
  HTTP entrypoint: `FORMR_BASE_URL=https://formr.example.org MCP_PUBLIC_URL=https://formr.example.org/mcp MCP_LOG_LEVEL=debug uv run http_server.py`.

## Testing

```bash
uv run --with pytest pytest tests/
```

## Data Protection

This server interacts with the formr **admin API** only. It accesses run structures, survey definitions, and settings — **never participant response data**. Runs are fetched and edited as structural JSON, not as data exports.

## License

MIT
