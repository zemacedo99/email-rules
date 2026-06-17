# rwz-importer

Parse Outlook **Classic** `.rwz` rule exports and recreate them as **server-side
Exchange inbox rules** in **New Outlook / Outlook on the web**, via the OWA REST API
(`https://outlook.office.com/api/v2.0/me/`).

## Why this exists

New Outlook (outlook.office.com) **cannot import** a Classic `.rwz` file, and importing
a `.rwz` into Outlook Classic signed in to a *different* mailbox fails too — the binary
embeds MAPI **EntryIDs** (X.500 `/o=.../cn=Recipients/cn=...`) bound to the mailbox that
created it. Moving rules between mailboxes by file simply does not work.

The only reliable path is to **read the `.rwz`, reconstruct the folders and rule logic,
and create the rules as server-side rules through the API**. That is what this tool does.

It is **generic**: nothing about any particular mailbox, address, folder, or rule name is
hardcoded. The bundled `.rwz` is used only as a test fixture. Mailbox-specific changes
(excluding a rule, remapping an address) are driven entirely by flags or by editing the
intermediate JSON.

## Two ways to use it

- **As a command-line tool** — the `parse → apply → validate → report` pipeline below.
- **As an MCP server an agent drives** — the same operations exposed as tools an AI agent
  (e.g. Claude Code) can call: inspect your folders, rules and recent mail, **suggest new
  rules** from patterns it spots, and **apply changes under a strict confirmation gate**.
  See [Use it from an agent (MCP server)](#use-it-from-an-agent-mcp-server).

Both share one engine, so the safety rules (dry-run first, never delete outside a
confirmed replace, never rewrite a cross-mailbox address silently) hold either way.

## Requirements

- Python ≥ 3.10
- A signed-in browser session on https://outlook.office.com to obtain a short-lived
  bearer token (see [Getting a token](#getting-a-token))

## Install

```powershell
# from the repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

This installs the `rwz-importer` console command. You can also run it as a module:
`python -m rwz_importer ...`.

The `dev` extra also pulls in the MCP SDK and the `rwz-importer-mcp` command used by the
[agent / MCP server](#use-it-from-an-agent-mcp-server). To get just that without the test
tooling, install the `mcp` extra: `pip install -e ".[mcp]"`.

## The pipeline

```
parse  →  (review / edit JSON)  →  apply (dry-run)  →  apply --yes  →  validate  →  report
```

| Command | What it does | Network? |
|---|---|---|
| `parse` | `.rwz` → rules JSON + summary table | No (always safe) |
| `apply` | Plan and (with `--yes`) create folders + rules | Yes (dry-run by default) |
| `validate` | Compare the live mailbox against an expected JSON | Yes |
| `report` | Render current folders + rules as Markdown/JSON | Yes |
| `suggest` | Read recent mail (metadata only) and propose rules for unfiled patterns | Yes (read-only) |
| `token-help` | Print how to get a bearer token | No |

### 1. Parse (offline, always safe)

```powershell
rwz-importer parse --file rules.rwz --output rules.json
```

Prints a table of the recovered rules and writes editable JSON. Review it — exclude a
rule by setting `"exclude": true`, or fix any address, before applying. Addresses that may
belong to a *different* mailbox are flagged with `addressMayBelongToDifferentMailbox` and
marked `[review]` in the table.

### 2. Getting a token

```powershell
rwz-importer token-help
```

Follow the **Network tab** method it prints: sign in to outlook.office.com, open DevTools
(F12) → Network, open a folder, find a request to `…/api/v2.0/me/…`, and copy the
`authorization` header value after `Bearer `. Then:

```powershell
$env:OWA_TOKEN = "<paste-token-here>"
```

The token is read **only** from `--token` or `$OWA_TOKEN`. It is **never written to disk**
by this tool. It expires after roughly an hour; re-extract if a run starts failing with
401.

### 3. Apply — dry-run first (the default)

`apply` makes **no changes** unless you pass `--yes` (or confirm interactively). The
default is a dry-run that prints the full plan: existing rules, folders to create, and a
`CREATE` / `REPLACE` / `SKIP` decision per rule with the assigned sequence.

```powershell
# dry-run (no changes) — this is the default
rwz-importer apply --file rules.rwz `
  --mailbox bob.sample@example.com `
  --exclude-rule ACMESUP `
  --map-address "alice.sample@example.org=bob.sample@example.org"
```

When the plan looks right, apply for real:

```powershell
rwz-importer apply --file rules.rwz `
  --mailbox bob.sample@example.com `
  --exclude-rule ACMESUP `
  --map-address "alice.sample@example.org=bob.sample@example.org" `
  --yes
```

`apply` runs post-apply validation automatically and prints the result.

You can also apply from an edited JSON instead of the `.rwz`:

```powershell
rwz-importer apply --from-json rules.json --yes
```

### 4. Validate and report

```powershell
rwz-importer validate --expected rules.json
rwz-importer report --format markdown --output report.md
```

### 5. Suggest rules from recent mail (read-only)

`suggest` samples recent message **metadata** (sender, subject, date — **never** bodies),
compares it against the rules already in place, and proposes new rules for traffic nothing
currently files. Three signals are mined: high-volume senders, domain clusters (several
senders at one domain), and recurring bracketed subject tags like `[JIRA]`.

```powershell
rwz-importer suggest --top 300            # human-readable
rwz-importer suggest --format json        # agent-friendly, includes ready-to-apply rules
```

It writes nothing. Feed an accepted suggestion back through the normal pipeline — save the
suggested rule into a `rules.json` and run `apply --from-json rules.json`.

## Transforms: excluding and remapping (no hardcoding)

There are two interchangeable ways to adjust rules for a target mailbox:

- **Flags** — `--exclude-rule NAME` (repeatable) and `--map-address OLD=NEW` (repeatable).
- **Edit the JSON** — set `"exclude": true`, or edit `conditions.fromAddresses`, then
  `apply --from-json`.

If a rule still carries an address flagged as cross-mailbox and you have **not** supplied a
mapping, `apply` will:

- **interactively** prompt you to skip / keep / remap it; or
- under `--yes` (or a non-interactive shell), **skip it with a warning** — it is never
  silently rewritten.

## Use it from an agent (MCP server)

The same engine is exposed over the **Model Context Protocol** so an agent (Claude Code, or
any MCP client) can manage a mailbox conversationally: "import these rules", "what patterns
am I not filing?", "show me the plan before changing anything".

Start the server (stdio transport):

```powershell
rwz-importer-mcp          # or: python -m rwz_importer.mcp_server
```

### Tools

| Tool | Writes? | Purpose |
|---|:--:|---|
| `parse_rwz_rules` | no (offline) | `.rwz` file → canonical rules JSON |
| `list_rules` / `list_folders` | no | Inspect the live mailbox |
| `read_messages` | no | Recent message **metadata** (never bodies) |
| `suggest_rules` | no | Propose rules for unfiled patterns; each carries a ready-to-apply rule |
| `report` | no | Current state as Markdown/JSON |
| `plan_apply` | **no** | Preview exactly what would change; returns a `confirmation_token` |
| `apply_plan` | **YES** | The only writing tool — gated (see below) |
| `token_help` | no | How to obtain a token |

### The token lives in the server's environment — never in chat

The server reads the bearer token from its own **`OWA_TOKEN` environment variable**; it is
**not** a tool argument, so it never flows through the agent's context or a transcript. Set
it in the MCP client's server config (and `OWA_BASE_URL` to override the endpoint):

```json
{
  "mcpServers": {
    "rwz-importer": {
      "command": "rwz-importer-mcp",
      "env": { "OWA_TOKEN": "<paste-token-here>" }
    }
  }
}
```

For Claude Code you can also add it from the shell:
`claude mcp add rwz-importer --env OWA_TOKEN=<token> -- rwz-importer-mcp`.

### Apply with confirmation (how a write is gated)

`apply_plan` is the single tool that mutates the mailbox, and it cannot write on a whim:

1. The agent calls `plan_apply` (or `apply_plan` with `confirm=false`) and gets a **dry-run
   plan** plus a `confirmation_token` — a digest of *exactly* the folders/rules that would
   be written.
2. The agent shows you that plan and gets your approval.
3. To write, it must call `apply_plan` with **`confirm=true`** and that **exact token**.
   If the mailbox changed since the plan was computed, the token no longer matches and the
   write is refused (`status: confirmation_required`) with a fresh token to re-confirm.

So a human always sees the plan first, the token can't be fabricated (it must come from a
real plan), and a drifted mailbox forces a re-plan. Cross-mailbox-flagged addresses are
**skipped** unless explicitly resolved with `map_addresses`, and a rule is deleted only as
part of a confirmed `REPLACE`.

## Safety guarantees

These hold whether you use the CLI or the MCP server:

- **Dry-run is the default.** The CLI writes nothing without `--yes`/confirmation (a
  non-interactive shell stays in dry-run instead of hanging); the MCP `apply_plan` writes
  nothing without `confirm=true` **and** a matching `confirmation_token`.
- **No credentials on disk or in chat.** The token comes from `--token`/`$OWA_TOKEN` (CLI)
  or the server's `OWA_TOKEN` env (MCP) only — never a tool argument, never written out.
- **Never deletes a rule** except as part of a confirmed `REPLACE`.
- **Never deletes a folder.** Folders are only ever created.
- **Cross-mailbox addresses are flagged**, never transformed (or applied) behind your back.

## Troubleshooting

- **401 / 403 from the API** — the token is expired or wrong-audience. OWA tokens last
  ~1 hour; re-run `token-help` and re-export. A token minted for
  `graph.microsoft.com` will **not** work here (this tool targets `outlook.office.com`).
- **"Sequence must be >= 1"** — Exchange rejects sequence 0; the tool catches this before
  sending. Let the planner assign sequences rather than forcing a 0.
- **A folder with an en/em dash** (e.g. `PRJX – Project Phoenix`) — created with the dash
  normalised to a hyphen by default. Use `--no-normalize-folders` to keep the original.
- **`pip install` fails behind a corporate proxy / OneDrive path** — install inside a
  local `.venv` (not a synced OneDrive folder) and set `HTTPS_PROXY` if required.

## Limitations & future work

- The parser is validated against the bundled fixture and the common rule types it
  contains (subject-contains, from-address, sender-contains). Other rule types
  (body, importance, sensitivity, message-class) are surfaced as
  `unsupportedConditions` warnings rather than silently dropped.
- Live `apply` / `validate` / `report` / `suggest` and the MCP server's live tools require a
  user-supplied token and act on a real mailbox, so they are covered by mocked / in-memory
  tests here, not live integration tests.
- `suggest` is heuristic (volume, domain clustering, subject tags) over a recent sample of
  message metadata; it is advisory and tuned by its `--min-*` thresholds.
- The mailbox layer is behind a `MailboxProvider` interface (the OWA client is one
  implementation), so a Graph or IMAP backend could slot in later without touching the
  engine, suggestion, or MCP layers.
- Future work: OAuth PKCE instead of a pasted token, more condition types,
  `.olk15Rules` (Outlook for Mac) support, and a small web UI.

## Development

```powershell
pytest tests/ -v
```
