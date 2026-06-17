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

## Safety guarantees

- **Dry-run is the default.** Nothing is written without `--yes` or an explicit
  confirmation. A non-interactive shell without `--yes` stays in dry-run instead of hanging.
- **No credentials on disk.** The token comes from `--token`/`$OWA_TOKEN` only.
- **Never deletes a rule** except as part of a confirmed `REPLACE`.
- **Never deletes a folder.** Folders are only ever created.
- **Cross-mailbox addresses are flagged**, never transformed behind your back.

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
- Live `apply` / `validate` / `report` require a user-supplied token and act on a real
  mailbox, so they are covered by mocked tests here, not live integration tests.
- Future work: OAuth PKCE instead of a pasted token, more condition types,
  `.olk15Rules` (Outlook for Mac) support, and a small web UI.

## Development

```powershell
pytest tests/ -v
```
