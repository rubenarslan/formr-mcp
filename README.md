# formr-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
[formr](https://formr.org) survey framework. It lets an AI assistant (Claude,
ChatGPT, Codex, Copilot in VS Code, Cursor, Gemini CLI and other MCP apps)
inspect, build and change formr runs and surveys, make and walk test sessions,
and look up formr's documentation, in conversation.

It runs in one of two ways:

| | Hosted | Local |
| --- | --- | --- |
| Who runs it | the formr instance, for all its researchers | you, on your computer |
| How you connect | add the instance's address to your app, sign in on formr | your app starts it; an API credential in `.env` |
| Apps | Claude, Claude Code, ChatGPT, Codex, VS Code | any MCP app that starts local servers |
| Needs | formr v2.0.0-rc13 or later, set up by its operators | Python 3.12+, [uv](https://docs.astral.sh/uv/), an API credential (admin level 2) |
| Extra tools | — | a run's structure as a file to edit; flowcharts |

## Hosted: add formr to your app by its address

Where the instance runs it, its address is `https://<formr admin host>/mcp`
(formr's Documentation → AI assistants shows it).

| App | How |
| --- | --- |
| Claude (web, desktop, mobile) | Settings → Connectors → Add custom connector, paste the address |
| Claude Code | `claude mcp add --transport http formr https://<host>/mcp`, then `/mcp` to sign in |
| ChatGPT (web) | add a custom MCP server with the address (under Plugins, or Settings → Apps with developer mode on, depending on the plan); authentication OAuth, registration by client metadata document (CIMD) if asked |
| Codex (CLI, IDE, app) | `codex mcp add formr --url https://<host>/mcp`, then `codex mcp login formr` |
| VS Code (GitHub Copilot) | Command Palette → *MCP: Add Server* → HTTP, or `.vscode/mcp.json`: `{"servers": {"formr": {"type": "http", "url": "https://<host>/mcp"}}}`; the tools are in agent mode |

The app sends you to formr to sign in (login, 2FA, admin level 2), where you
choose the connection's scopes and runs, as for an API credential. formr is the
OAuth server: it knows each app by its client ID metadata document (on
`claude.ai`, `chatgpt.com` or `vscode.dev`; formr's `oauth_client_metadata_hosts`),
requires PKCE, and lists each connection under Account → API, where you narrow
or delete it (deleting ends it at once).

Apps that register themselves with every server instead (dynamic client
registration: Cursor and Gemini CLI, in November 2026) can't sign in this way;
use the [local server](#local-run-it-on-your-computer).

Every request carries the connection's token; the server checks it with formr
(`GET /api/v1/user/token`) and calls the API with it, so formr's scopes, run
allowlist and run ownership decide every call. The server keeps no credential,
secret or file: the editing tools fetch the run from formr, edit, validate and
upload it in one step, and `get_run_structure_to_file`,
`update_run_structure_from_file` and `open_flowchart` are not offered.

### Running the hosted server (operators)

- **formr-docker**: append `:docker-compose-mcp.yml` to `COMPOSE_FILE`, then
  `./update.sh`. The layer runs `http_server.py` from the `formr-mcp` image, sets
  formr's `mcp_url` and routes `/mcp` and formr's OAuth endpoints on the admin host.
- **Elsewhere**: `uv run http_server.py` (or the `Dockerfile`), behind HTTPS, with:

| Variable | |
| --- | --- |
| `FORMR_BASE_URL` | formr's admin origin, the issuer (e.g. `https://formr.example.org`) |
| `MCP_PUBLIC_URL` | this server's address, formr's `mcp_url` (e.g. `https://formr.example.org/mcp`); it answers only requests for this host |
| `FORMR_DATA_ACCESS` | the ceiling for every researcher: `test_only` (default) or `all` ([Participants' data](#participants-data)) |
| `MCP_HTTP_HOST`, `MCP_HTTP_PORT` | where it listens (`0.0.0.0`, `8000`) |
| `MCP_FORWARDED_ALLOW_IPS` | proxies whose `X-Forwarded-*` headers it takes (`*`) |
| `FORMR_HTTP_TIMEOUT`, `FORMR_MCP_LOG_LEVEL` | as for the local server |

Route `/mcp` to it, and `/.well-known/oauth-authorization-server` and
`/api/oauth/` on the admin host to formr, and set `$settings['mcp_url']`. Claude's
and ChatGPT's requests come from their companies' shared addresses, for all
their users at once: don't IP-ban them, and rate-limit per address generously.
formr's INSTALLATION.md ("AI assistants") has the details.

## Local: run it on your computer

Works with any formr instance and any MCP app that starts local servers.

1. Make an API credential under Account → API in formr (needs admin level 2).
   Give it the scopes and runs it needs: `run:read`, `run:write`, `survey:read`,
   `survey:write` to design runs; `session:read`, `session:write`, `data:read`
   for test sessions; `file:read`, `file:write` for run files; `run:delete` only
   to delete runs. Its secret is a password: keep it out of shared folders.
2. Install:

   ```bash
   git clone https://github.com/rubenarslan/formr-mcp && cd formr-mcp
   cp .env.example .env    # then fill in FORMR_BASE_URL, FORMR_CLIENT_ID, FORMR_CLIENT_SECRET
   uv sync
   ```

3. Add it to your app (below). The server reads `.env` next to `server.py`.

| Variable | |
| --- | --- |
| `FORMR_BASE_URL` | the formr instance (e.g. `https://formr.example.org`) |
| `FORMR_CLIENT_ID` | the credential's 32-character client ID |
| `FORMR_CLIENT_SECRET` | its 64-character secret |
| `FORMR_DATA_ACCESS` | (optional) `test_only` (default) or `all` ([Participants' data](#participants-data)) |
| `FORMR_WORKSPACE_DIR` | (optional) where run files are kept (default `.formr/` in the app's working directory) |
| `FORMR_HTTP_TIMEOUT` | (optional) seconds per request to formr (default `60`) |
| `FORMR_MCP_LOG_LEVEL` | (optional) `INFO` (default) or `DEBUG` |
| `FLOWCHART_URL` | (optional) the formr Flowchart Generator `open_flowchart` uploads to |

Replace `/path/to/formr-mcp` below with where you cloned it.

**Claude Desktop**: `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`):

```json
{"mcpServers": {"formr": {"command": "uv", "args": ["run", "--directory", "/path/to/formr-mcp", "server.py"]}}}
```

**Claude Code**:

```bash
claude mcp add formr -- uv run --directory /path/to/formr-mcp server.py
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.formr]
command = "uv"
args = ["run", "--directory", "/path/to/formr-mcp", "server.py"]
```

**VS Code** (`.vscode/mcp.json`):

```json
{"servers": {"formr": {"type": "stdio", "command": "uv", "args": ["run", "--directory", "/path/to/formr-mcp", "server.py"]}}}
```

**Cursor** (`.cursor/mcp.json`) and **Gemini CLI** (`~/.gemini/settings.json`):

```json
{"mcpServers": {"formr": {"command": "uv", "args": ["run", "--directory", "/path/to/formr-mcp", "server.py"]}}}
```

**opencode** (`opencode.json` in your project; `opencode.json.example` has it):

```json
{"mcp": {"formr": {"type": "local", "command": ["uv", "run", "--directory", "/path/to/formr-mcp", "server.py"], "enabled": true}}}
```

ChatGPT on the web reaches only servers on the internet: use the hosted server.

## Participants' data

The read tools (sessions, results, logs, files) pass on what `FORMR_DATA_ACCESS`
allows, whatever the credential's or connection's scopes: `test_only`, the
default, only test sessions (`testing=1`); `all`, real participants' too. Per-call
filters apply within that ceiling and never exceed it; `whoami` reports it. On a
hosted server the operator sets it for everyone. What the tools return goes to the
AI company that runs the assistant. Run secrets reach it by name only.

## Changing a live run

Uploads change only what changed (formr v2.0.0-rc13 and later):

- `get_run_structure` gives each unit its `run_unit_id`. A unit sent back with it
  stays that unit: its participants stay on it, and move with it when its
  position changes. A unit without one is new; the editing tools take care of
  this (`duplicate_run_units` drops it from copies).
- The structure's `version` makes formr refuse an upload made from a copy that
  changed in formr meanwhile (fetch again).
- A unit left out is removed, and its participants go on from whatever now stands
  in its place. Where real participants are at it, formr refuses the upload until
  it is repeated with `confirm_removal=true` (`remove_run_unit`,
  `update_run_structure`, `update_run_structure_from_file`): the assistant is told
  to ask you first.
- Survey items are saved only where they differ, and that needs `survey:write`.

An older formr replaces every unit on each upload and restarts every
participant's current step.

## Tools

Each tool needs the scope formr checks for its API call; `whoami` lists what
the connection or credential may use. *Local only*: not offered by a hosted server.

| Group | Tools |
| --- | --- |
| Account | `whoami` (scopes, run allowlist, data ceiling, which tools work; call it first) |
| Runs | `list_runs`, `get_run`, `create_run`, `update_run_settings`, `delete_run` (`run:delete`) |
| Structure | `get_run_structure`, `update_run_structure` (JSON text); `get_run_structure_to_file`, `update_run_structure_from_file` (*local only*: `.formr/<name>.json`) |
| Editing | `add_run_unit`, `remove_run_unit`, `duplicate_run_units`, `shift_run_positions`, `renormalize_positions` (each remaps branch targets and Wait positions) |
| Checking | `summarize_run`, `find_run_items`, `analyze_run` (branch flow, references, item consistency; R syntax where R is installed), `open_flowchart` (*local only*: uploads the structure to the Flowchart Generator, a link for 24 hours) |
| Surveys | `get_survey_structure`, `fetch_google_sheet` (a sheet shared by link, as CSV text; XLSX and files *local only*) |
| Test sessions | `create_test_sessions` (with their links), `test_session_action` (advance, execute, move to a position, end an External), `get_run_logs` (unit, email and push logs) |
| Participants' data | `list_sessions`, `get_session`, `list_unit_sessions`, `get_run_results` (within the [data ceiling](#participants-data)) |
| Run files | `list_run_files`, `upload_run_file` (text or base64; a workspace file *local only*), `delete_run_file` |
| Help | `get_documentation`, `get_documentation_topics`, `get_documentation_sections`, `get_unit_types`, `list_patterns`, `get_pattern` |

Documentation topics: `item-types`, `run-concepts`, `r-code`, `survey-json`,
`examples`, `editing-tools`, `best-practices`, `unit-types-advanced`,
`data-access`, `patterns`, `custom-r-and-secrets`, `form-v2`.

### Editing with files (local)

With file access (a coding agent or editor), large runs are easier as a file:

```
1. get_run_structure_to_file("my-run")       → .formr/my-run.json (the old one kept as .bak)
2. edit .formr/my-run.json
3. update_run_structure_from_file("my-run")  → validates, uploads, removes the .bak
```

The editing and checking tools then work on that file. Without file access, or
on a hosted server: `get_run_structure`, edit the JSON in the reply,
`update_run_structure`.

## Logs

The server logs to **stderr** (stdout is the local server's JSON-RPC channel).
At `INFO` it logs each tool call (name, argument preview, duration, outcome) and
each formr API request (status, duration); `DEBUG` adds httpx's lines.

- **Claude Desktop**: `~/Library/Logs/Claude/mcp-server-<name>.log` (macOS),
  `%APPDATA%\Claude\logs\mcp-server-<name>.log` (Windows).
- **Claude Code**: `claude --debug`; `/mcp` shows the status.
- **Other apps**: their MCP or output panel (VS Code: the server's *Show Output*).
- **Directly**: `npx @modelcontextprotocol/inspector uv run server.py`. The hosted
  server answers only for `MCP_PUBLIC_URL`'s host, so to try it on your computer
  set that to the address you call it at (`MCP_PUBLIC_URL=http://localhost:8000/mcp`);
  signing in then needs a formr whose `mcp_url` is that address.

## Development

```bash
uv run --with pytest pytest tests/
```

`server.py` defines the tools; `http_server.py` is the hosted entry point
(token check: `formr_mcp/auth.py`). `formr_mcp/`: `client.py` (formr's API),
`validation.py`, `editing.py`, `analysis.py`, `summarize.py`, `data_access.py`
(the data ceiling), `documentation.py` and `patterns.py` (the help topics),
`google_sheets.py`, `logsetup.py`.

## License

MIT
