"""``click`` entry point: ``parse`` / ``apply`` / ``validate`` / ``report`` / ``login`` / ``token-help`` / ``web``.

Design priorities are safety and transparency:

* **Dry-run is the default for ``apply``** — nothing is written without an explicit
  ``--yes`` or an interactive confirmation. A non-interactive session without
  ``--yes`` is downgraded to a dry-run rather than hanging on a prompt.
* The **token is read from ``--token`` or ``$OWA_TOKEN`` only** and is never written
  to disk or echoed back.
* Existing rules are deleted **only** as part of a confirmed ``REPLACE``; folders are
  never deleted. Addresses flagged as possibly cross-mailbox are never rewritten
  silently — they are resolved interactively or skipped with a warning under ``--yes``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from rwz_importer.api import registry
from rwz_importer.api.errors import MailboxApiError
from rwz_importer.api.owa_client import ExistingRule, Folder, normalize_folder_name
from rwz_importer.api.provider import MailboxProvider
from rwz_importer.engine import (
    ActionKind,
    ApplyPlan,
    ConflictDecision,
    Decision,
    PlannedAction,
    RuleSuggestion,
    build_plan,
    detect_conflicts,
    suggest_rules,
    validate_state,
)
from rwz_importer.logging_setup import setup_logging
from rwz_importer.models import RuleSet
from rwz_importer.parser import parse_rwz_file
from rwz_importer.report.generator import generate_json_report, generate_markdown_report


@click.group()
@click.version_option(package_name="rwz-importer", message="%(version)s")
def cli() -> None:
    """Email Rule Manager - manage server-side inbox rules across Outlook, Gmail, and IMAP/Sieve.

    Pick a backend with ``--provider`` (outlook | gmail | imap); also imports Outlook
    Classic ``.rwz`` exports into any of them.
    """


# --------------------------------------------------------------------- parse --
@cli.command()
@click.option(
    "--file",
    "rwz_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the .rwz rule export.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the parsed rules to this JSON file (otherwise stdout only).",
)
@click.option("--verbose", is_flag=True, help="Enable DEBUG logging.")
def parse(rwz_file: Path, output: Path | None, verbose: bool) -> None:
    """Parse a ``.rwz`` file into rules JSON. Offline and always safe."""
    setup_logging(verbose=verbose)
    ruleset = parse_rwz_file(rwz_file)
    _print_rule_summary(ruleset)

    if output:
        output.write_text(ruleset.to_json(), encoding="utf-8")
        click.echo(f"\nWrote {len(ruleset.rules)} rules to {output}")
    else:
        click.echo("\n(Pass --output to save this as editable JSON.)")


# --------------------------------------------------------------------- apply --
@cli.command()
@click.option(
    "--file",
    "rwz_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Parse rules from this .rwz file.",
)
@click.option(
    "--from-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Load rules from a previously parsed (and possibly edited) JSON file.",
)
@click.option(
    "--token",
    envvar=["MAIL_TOKEN", "OWA_TOKEN"],
    default=None,
    help="Access token. Read from $MAIL_TOKEN/$OWA_TOKEN if omitted. Never stored.",
)
@click.option("--mailbox", default=None, help="Target mailbox address (for reporting only).")
@click.option(
    "--exclude-rule",
    "exclude_rules",
    multiple=True,
    metavar="NAME",
    help="Drop a rule by name (repeatable).",
)
@click.option(
    "--map-address",
    "map_addresses",
    multiple=True,
    metavar="OLD=NEW",
    help="Rewrite a from-address (repeatable).",
)
@click.option(
    "--normalize-folders/--no-normalize-folders",
    default=True,
    help="Map Unicode punctuation in folder names to ASCII when creating folders.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Preview only (default). Use --no-dry-run (or --yes) to write changes.",
)
@click.option("--yes", is_flag=True, help="Apply non-interactively (implies --no-dry-run).")
@click.option("--base-url", default=None, help="Override the API base URL (default: the provider's).")
@click.option("--provider", default=registry.DEFAULT_PROVIDER, show_default=True,
              help="Mailbox provider: outlook, gmail, or imap.")
@click.option("--verbose", is_flag=True, help="Enable DEBUG logging.")
@click.option(
    "--logfile",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Mirror logs to this file for an audit trail.",
)
def apply(
    rwz_file: Path | None,
    from_json: Path | None,
    token: str | None,
    mailbox: str | None,
    exclude_rules: tuple[str, ...],
    map_addresses: tuple[str, ...],
    normalize_folders: bool,
    dry_run: bool,
    yes: bool,
    base_url: str | None,
    provider: str,
    verbose: bool,
    logfile: Path | None,
) -> None:
    """Parse/load rules, show a plan, and (with --yes / confirmation) apply it."""
    setup_logging(verbose=verbose, logfile=logfile)

    if bool(rwz_file) == bool(from_json):
        raise click.UsageError("Pass exactly one of --file or --from-json.")

    ruleset = parse_rwz_file(rwz_file) if rwz_file else _load_json(from_json)
    address_map = _parse_address_map(map_addresses)
    excludes = set(exclude_rules)

    # --yes means the user wants to write; never silently honour it as a no-op.
    if yes:
        dry_run = False
    interactive = sys.stdin.isatty()

    # Decide whether we will reach the server at all.
    if not token and not _is_imap(provider):
        if not dry_run:
            raise click.UsageError(
                "apply needs a token to write changes. Pass --token or set "
                "MAIL_TOKEN/OWA_TOKEN, or run with --dry-run for an offline preview."
            )
        _offline_preview(ruleset, excludes, address_map, normalize_folders)
        return

    try:
        with registry.build_provider(provider, _provider_config(provider, token, base_url)) as client:
            existing_rules = client.list_rules()
            folders = _all_folders(client)
            folder_names = _folder_name_set(folders)

            decisions = detect_conflicts(ruleset.rules, existing_rules, folder_names)
            base_sequence = _next_sequence(existing_rules)
            plan = build_plan(
                decisions,
                excludes=excludes,
                address_map=address_map,
                normalize_folders=normalize_folders,
                base_sequence=base_sequence,
            )
            _print_plan(plan, existing_rules=existing_rules, mailbox=mailbox)

            if dry_run:
                click.echo("\nDry-run: no changes made. Re-run with --yes to apply.")
                return

            if not yes:
                if not interactive:
                    click.echo(
                        "\nNon-interactive session without --yes; staying in dry-run."
                    )
                    return
                if not click.confirm("\nApply these changes to the mailbox?", default=False):
                    click.echo("Aborted; no changes made.")
                    return

            _resolve_flagged_actions(plan, interactive=interactive, assume_yes=yes)
            _execute_plan(client, plan, folders)

            # Post-apply validation against what we intended to create.
            live_rules = client.list_rules()
            result = validate_state(live_rules, ruleset)
            click.echo("\n" + result.summary)
            for status in result.statuses:
                mark = "OK  " if status.ok else "FAIL"
                click.echo(f"  [{mark}] {status.name}: {status.detail}")
    except MailboxApiError as exc:
        raise click.ClickException(_format_api_error(exc)) from exc


# ------------------------------------------------------------------ validate --
@cli.command()
@click.option("--token", envvar=["MAIL_TOKEN", "OWA_TOKEN"], default=None,
              help="Access token ($MAIL_TOKEN/$OWA_TOKEN). Never stored.")
@click.option(
    "--expected",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default="rules.json",
    show_default=True,
    help="Parsed rules JSON to validate the live mailbox against.",
)
@click.option("--mailbox", default=None, help="Target mailbox address (for reporting only).")
@click.option("--base-url", default=None, help="Override the API base URL (default: the provider's).")
@click.option("--provider", default=registry.DEFAULT_PROVIDER, show_default=True,
              help="Mailbox provider: outlook, gmail, or imap.")
@click.option("--verbose", is_flag=True, help="Enable DEBUG logging.")
def validate(
    token: str | None, expected: Path, mailbox: str | None, base_url: str | None,
    provider: str, verbose: bool,
) -> None:
    """Compare the live mailbox rules against an expected rules JSON."""
    setup_logging(verbose=verbose)
    if not token and not _is_imap(provider):
        raise click.UsageError("validate needs a token. Pass --token or set MAIL_TOKEN/OWA_TOKEN.")

    ruleset = _load_json(expected)
    try:
        with registry.build_provider(provider, _provider_config(provider, token, base_url)) as client:
            live_rules = client.list_rules()
    except MailboxApiError as exc:
        raise click.ClickException(_format_api_error(exc)) from exc

    result = validate_state(live_rules, ruleset)
    click.echo(result.summary)
    for status in result.statuses:
        mark = "OK  " if status.ok else "FAIL"
        click.echo(f"  [{mark}] {status.name}: {status.detail}")
    if result.extra_rules:
        click.echo("\nUnexpected rules on server: " + ", ".join(result.extra_rules))
    if not result.ok:
        sys.exit(1)


# -------------------------------------------------------------------- report --
@cli.command()
@click.option("--token", envvar=["MAIL_TOKEN", "OWA_TOKEN"], default=None,
              help="Access token ($MAIL_TOKEN/$OWA_TOKEN). Never stored.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    show_default=True,
    help="Report format.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the report here (otherwise stdout).",
)
@click.option("--mailbox", default=None, help="Target mailbox address (for reporting only).")
@click.option("--base-url", default=None, help="Override the API base URL (default: the provider's).")
@click.option("--provider", default=registry.DEFAULT_PROVIDER, show_default=True,
              help="Mailbox provider: outlook, gmail, or imap.")
@click.option("--verbose", is_flag=True, help="Enable DEBUG logging.")
def report(
    token: str | None,
    fmt: str,
    output: Path | None,
    mailbox: str | None,
    base_url: str | None,
    provider: str,
    verbose: bool,
) -> None:
    """Fetch the live mailbox state and render it as Markdown or JSON."""
    setup_logging(verbose=verbose)
    if not token and not _is_imap(provider):
        raise click.UsageError("report needs a token. Pass --token or set MAIL_TOKEN/OWA_TOKEN.")

    try:
        with registry.build_provider(provider, _provider_config(provider, token, base_url)) as client:
            rules = client.list_rules()
            folders = _all_folders(client)
    except MailboxApiError as exc:
        raise click.ClickException(_format_api_error(exc)) from exc

    if fmt == "json":
        text = generate_json_report(rules, folders, mailbox=mailbox)
    else:
        text = generate_markdown_report(rules, folders, mailbox=mailbox)

    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(f"Wrote {fmt} report to {output}")
    else:
        click.echo(text)


# ------------------------------------------------------------------- suggest --
@cli.command()
@click.option("--token", envvar=["MAIL_TOKEN", "OWA_TOKEN"], default=None,
              help="Access token ($MAIL_TOKEN/$OWA_TOKEN). Never stored.")
@click.option("--folder", default="Inbox", show_default=True, help="Folder to sample.")
@click.option(
    "--top",
    default=300,
    show_default=True,
    help="How many recent messages to read (metadata only).",
)
@click.option("--min-sender", default=3, show_default=True, help="Min messages for a sender rule.")
@click.option("--min-domain", default=6, show_default=True, help="Min messages for a domain rule.")
@click.option(
    "--min-subject", default=4, show_default=True, help="Min messages for a subject-tag rule."
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format (json is agent-friendly).",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write suggestions here (otherwise stdout).",
)
@click.option("--base-url", default=None, help="Override the API base URL (default: the provider's).")
@click.option("--provider", default=registry.DEFAULT_PROVIDER, show_default=True,
              help="Mailbox provider: outlook, gmail, or imap.")
@click.option("--verbose", is_flag=True, help="Enable DEBUG logging.")
def suggest(
    token: str | None,
    folder: str,
    top: int,
    min_sender: int,
    min_domain: int,
    min_subject: int,
    fmt: str,
    output: Path | None,
    base_url: str | None,
    provider: str,
    verbose: bool,
) -> None:
    """Read recent mail and propose rules for unfiled patterns. Read-only.

    Reads message metadata only (sender, subject, date — never bodies), compares
    against the rules already in place, and prints suggested rules. Nothing is
    applied: feed an accepted suggestion back through `apply --from-json`.
    """
    setup_logging(verbose=verbose)
    if not token and not _is_imap(provider):
        raise click.UsageError("suggest needs a token. Pass --token or set MAIL_TOKEN/OWA_TOKEN.")

    try:
        with registry.build_provider(provider, _provider_config(provider, token, base_url)) as client:
            messages = client.list_messages(folder=folder, top=top)
            existing = client.list_rules()
    except MailboxApiError as exc:
        raise click.ClickException(_format_api_error(exc)) from exc

    suggestions = suggest_rules(
        messages,
        existing,
        min_sender=min_sender,
        min_domain=min_domain,
        min_subject=min_subject,
    )

    if fmt == "json":
        text = json.dumps(
            {
                "folder": folder,
                "sampled_messages": len(messages),
                "suggestions": [s.to_dict() for s in suggestions],
            },
            indent=2,
            ensure_ascii=False,
        )
    else:
        text = _render_suggestions(suggestions, sampled=len(messages), folder=folder)

    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(f"Wrote {len(suggestions)} suggestions to {output}")
    else:
        click.echo(text)


# ---------------------------------------------------------------- token-help --
@cli.command(name="token-help")
def token_help() -> None:
    """Print how to obtain an OWA bearer token from a signed-in browser session."""
    click.echo(_TOKEN_HELP)


# --------------------------------------------------------------------- login --
@cli.command()
@click.option("--provider", default=registry.DEFAULT_PROVIDER, show_default=True,
              help="Which provider to sign in to: outlook (Microsoft) or gmail (Google).")
@click.option("--tenant", default=None, help="AAD tenant (Outlook; default: common, or $OWA_OAUTH_TENANT).")
@click.option("--client-id", default=None,
              help="OAuth client id (Outlook: $OWA_OAUTH_CLIENT_ID; Gmail: $GOOGLE_OAUTH_CLIENT_ID).")
@click.option("--client-secret", default=None,
              help="OAuth client secret (Gmail Desktop client; $GOOGLE_OAUTH_CLIENT_SECRET).")
@click.option("--export/--no-export", "show_export", default=True, show_default=True,
              help="Also print a ready-to-paste MAIL_TOKEN line.")
def login(
    provider: str,
    tenant: str | None,
    client_id: str | None,
    client_secret: str | None,
    show_export: bool,
) -> None:
    """Sign in (Microsoft device-code or Google OAuth) and print an access token.

    Nothing is stored: the token lives only in your shell after you export it. Outlook
    uses the device-code flow (a short code + URL); Gmail opens your browser for Google's
    consent screen and captures the redirect on a local loopback port (needs your own
    Desktop OAuth client id + secret).
    """
    from rwz_importer import auth, auth_google

    canon = registry.canonical_provider(provider)
    if canon == "gmail":
        try:
            cid, secret = auth_google.credentials(client_id, client_secret)
        except auth_google.GoogleAuthError as exc:
            raise click.UsageError(str(exc)) from exc

        def gprompt(url: str) -> None:
            click.echo("\nOpening your browser to sign in with Google. If it doesn't open, visit:")
            click.echo(f"    {url}")
            click.echo("\nWaiting for you to finish signing in...  (Ctrl+C to cancel)")

        try:
            tokens = auth_google.run_loopback_flow(
                client_id=cid, client_secret=secret, on_prompt=gprompt
            )
        except auth_google.GoogleAuthError as exc:
            raise click.UsageError(str(exc)) from exc
        except KeyboardInterrupt:  # pragma: no cover - interactive
            raise click.Abort() from None
        token = tokens["access_token"]
        click.echo("\nSigned in with Google. Access token (valid ~1 hour):")
    else:
        def prompt(info: dict) -> None:
            url = info.get("verification_uri") or "https://microsoft.com/devicelogin"
            click.echo("")
            click.echo("To sign in, open this page in a browser:")
            click.echo(f"    {url}")
            click.echo(f"and enter the code:  {info.get('user_code', '')}")
            click.echo("")
            click.echo("Waiting for you to finish signing in...  (Ctrl+C to cancel)")

        try:
            token = auth.acquire_token(on_prompt=prompt, tenant=tenant, client_id=client_id)
        except auth.AuthError as exc:
            raise click.UsageError(
                f"{exc}\n\nIf your organisation blocks device-code sign-in, use "
                "`rwz-importer token-help` for the manual (Network tab) method."
            ) from exc
        except KeyboardInterrupt:  # pragma: no cover - interactive
            raise click.Abort() from None
        click.echo("\nSigned in. OWA token (valid ~1 hour):")

    click.echo("")
    click.echo(token)
    if show_export:
        click.echo("")
        click.echo("Use it with this tool — PowerShell:")
        click.echo(f'    $env:MAIL_TOKEN = "{token}"')
        click.echo("bash / zsh:")
        click.echo(f'    export MAIL_TOKEN="{token}"')
        if canon == "gmail":
            click.echo("(Add --provider gmail to apply/validate/report/suggest.)")


# --------------------------------------------------------------------- web --
@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host (loopback only).")
@click.option("--port", default=8765, show_default=True, type=int, help="Bind port.")
def web(host: str, port: int) -> None:
    """Launch the local web UI (instructions, rules CRUD, run, suggestions, import).

    The token is entered in the browser and held only in the server's memory. Needs
    the web extra: ``pip install -e ".[web]"``.
    """
    try:
        from rwz_importer.web.app import run as run_web
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extras
        raise click.UsageError(
            f"The web UI needs extra packages ({exc.name}). Install them with: "
            'pip install -e ".[web]"'
        ) from exc
    run_web(host=host, port=port)


# --------------------------------------------------------------------- helpers --
def _load_json(path: Path) -> RuleSet:
    return RuleSet.from_json(path.read_text(encoding="utf-8"))


def _parse_address_map(pairs: tuple[str, ...]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.UsageError(f"--map-address expects OLD=NEW, got {pair!r}.")
        old, new = pair.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise click.UsageError(f"--map-address expects OLD=NEW, got {pair!r}.")
        mapping[old] = new
    return mapping


def _is_imap(provider: str) -> bool:
    return registry.canonical_provider(provider) == "imap"


def _provider_config(provider: str, token: str | None, base_url: str | None) -> dict:
    """Credentials for the registry. Token providers use --token/--base-url; IMAP reads
    MAIL_IMAP_* / MAIL_SIEVE_* env vars (host/username/password) since it has no token."""
    if _is_imap(provider):
        return {
            "host": os.environ.get("MAIL_IMAP_HOST", ""),
            "port": int(os.environ.get("MAIL_IMAP_PORT") or 993),
            "username": os.environ.get("MAIL_IMAP_USERNAME", ""),
            "password": os.environ.get("MAIL_IMAP_PASSWORD", ""),
            "use_ssl": os.environ.get("MAIL_IMAP_SSL", "1") not in ("0", "false", "False"),
            "sieve_host": os.environ.get("MAIL_SIEVE_HOST", ""),
            "sieve_port": int(os.environ.get("MAIL_SIEVE_PORT") or 4190),
        }
    return {"token": token, "base_url": base_url}


def _all_folders(client: MailboxProvider) -> list[Folder]:
    """Inbox child folders plus top-level folders, de-duplicated by id."""
    seen: dict[str, Folder] = {}
    for folder in client.list_inbox_folders() + client.list_all_folders():
        if folder.id and folder.id not in seen:
            seen[folder.id] = folder
    return list(seen.values())


def _folder_name_set(folders: list[Folder]) -> set[str]:
    return {f.display_name.strip().lower() for f in folders if f.display_name}


def _next_sequence(existing_rules: list[ExistingRule]) -> int:
    seqs = [r.sequence for r in existing_rules if r.sequence is not None]
    return max(seqs) + 1 if seqs else 1


def _print_rule_summary(ruleset: RuleSet) -> None:
    click.echo(f"Parsed {len(ruleset.rules)} rules from {ruleset.source_file or '?'}")
    if ruleset.source_mailbox:
        click.echo(f"Source mailbox: {ruleset.source_mailbox}")
    click.echo("")
    header = f"{'Seq':>3}  {'Rule':<32}  {'Move to':<22}  Conditions"
    click.echo(header)
    click.echo("-" * len(header))
    for rule in ruleset.rules:
        seq = rule.sequence if rule.sequence is not None else "-"
        dest = rule.actions.moveToFolder or "-"
        flag = "  [review]" if rule.flags.addressMayBelongToDifferentMailbox else ""
        click.echo(
            f"{seq:>3}  {_clip(rule.name, 32):<32}  {_clip(dest, 22):<22}  "
            f"{_conditions_text(rule)}{flag}"
        )


def _conditions_text(rule) -> str:
    return _condition_text(rule.conditions)


def _condition_text(cond) -> str:
    parts: list[str] = []
    if cond.fromAddresses:
        parts.append("from " + ", ".join(cond.fromAddresses))
    if cond.subjectContains:
        parts.append("subject " + ", ".join(cond.subjectContains))
    if cond.senderContains:
        parts.append("sender " + ", ".join(cond.senderContains))
    return "; ".join(parts) if parts else "(none)"


def _print_plan(
    plan: ApplyPlan, *, existing_rules: list[ExistingRule], mailbox: str | None
) -> None:
    if mailbox:
        click.echo(f"Mailbox: {mailbox}")
    click.echo(f"Existing rules on server: {len(existing_rules)}")
    click.echo("")

    folders = plan.folder_creations
    if folders:
        click.echo("Folders to create:")
        for action in folders:
            extra = ""
            if action.folder_create_name and action.folder_create_name != action.folder_display:
                extra = f" (as {action.folder_create_name!r})"
            click.echo(f"  + {action.folder_display}{extra}")
        click.echo("")

    click.echo("Rules:")
    for action in plan.actions:
        if action.kind is ActionKind.CREATE_FOLDER:
            continue
        verb = {
            ActionKind.CREATE_RULE: "CREATE ",
            ActionKind.REPLACE_RULE: "REPLACE",
            ActionKind.SKIP_RULE: "SKIP   ",
        }[action.kind]
        name = action.rule.name if action.rule else "?"
        seq = f"seq {action.sequence}" if action.sequence is not None else ""
        review = "  [needs review: cross-mailbox address]" if action.needs_review else ""
        click.echo(f"  {verb}  {_clip(name, 36):<36} {seq:<8} {action.reason}{review}")


def _resolve_flagged_actions(
    plan: ApplyPlan, *, interactive: bool, assume_yes: bool
) -> None:
    """Resolve cross-mailbox-flagged rules before writing.

    Under ``--yes`` flagged rules are converted to skips (never rewritten silently).
    Interactively the user chooses skip / keep / remap per rule.
    """
    for action in plan.actions:
        if not action.needs_review or action.rule is None:
            continue
        rule = action.rule
        addrs = ", ".join(rule.conditions.fromAddresses) or "(none)"

        if assume_yes or not interactive:
            action.kind = ActionKind.SKIP_RULE
            action.needs_review = False
            action.reason = "Skipped: unresolved cross-mailbox address (use --map-address)."
            click.echo(f"  ! Skipping {rule.name!r}: cross-mailbox address {addrs}")
            continue

        click.echo(
            f"\nRule {rule.name!r} has an address that may belong to a different "
            f"mailbox: {addrs}"
        )
        choice = click.prompt(
            "  [s]kip / [k]eep as-is / [r]emap address", default="s"
        ).strip().lower()
        if choice.startswith("r"):
            old = click.prompt("    address to replace").strip()
            new = click.prompt("    replacement address").strip()
            rule.conditions.fromAddresses = [
                new if a.strip().lower() == old.lower() else a
                for a in rule.conditions.fromAddresses
            ]
            rule.flags.addressMayBelongToDifferentMailbox = False
            action.needs_review = False
        elif choice.startswith("k"):
            rule.flags.addressMayBelongToDifferentMailbox = False
            action.needs_review = False
        else:
            action.kind = ActionKind.SKIP_RULE
            action.needs_review = False
            action.reason = "Skipped by user (cross-mailbox address)."


def _execute_plan(client: MailboxProvider, plan: ApplyPlan, existing_folders: list[Folder]) -> None:
    """Create folders, then create/replace rules. Deletes only inside a REPLACE."""
    folder_ids: dict[str, str] = {}
    for folder in existing_folders:
        if folder.display_name:
            folder_ids[folder.display_name.strip().lower()] = folder.id
            folder_ids[normalize_folder_name(folder.display_name).strip().lower()] = folder.id

    for action in plan.folder_creations:
        name = action.folder_create_name or action.folder_display or ""
        created = client.create_folder(name)
        click.echo(f"  created folder {created.display_name!r}")
        folder_ids[created.display_name.strip().lower()] = created.id
        if action.folder_display:
            folder_ids[action.folder_display.strip().lower()] = created.id

    created_rules = 0
    for action in plan.actions:
        if action.kind not in (ActionKind.CREATE_RULE, ActionKind.REPLACE_RULE):
            continue
        rule = action.rule
        assert rule is not None
        folder_id = _resolve_folder_id(action.folder_display, folder_ids)

        if action.kind is ActionKind.REPLACE_RULE and action.existing_rule_id:
            client.delete_rule(action.existing_rule_id)
            click.echo(f"  replaced (deleted old) {rule.name!r}")

        client.create_rule(
            display_name=rule.name,
            sequence=action.sequence or 1,
            folder_id=folder_id,
            from_addresses=rule.conditions.fromAddresses or None,
            subject_contains=rule.conditions.subjectContains or None,
            sender_contains=rule.conditions.senderContains or None,
            stop_processing=rule.actions.stopProcessingRules,
            enabled=rule.enabled,
            archive=rule.actions.archive,
        )
        created_rules += 1
        click.echo(f"  created rule {rule.name!r} (seq {action.sequence})")

    click.echo(f"\nApplied: {len(plan.folder_creations)} folders, {created_rules} rules.")


def _resolve_folder_id(display: str | None, folder_ids: dict[str, str]) -> str | None:
    if not display:
        return None
    for key in (display.strip().lower(), normalize_folder_name(display).strip().lower()):
        if key in folder_ids:
            return folder_ids[key]
    return None


def _offline_preview(
    ruleset: RuleSet,
    excludes: set[str],
    address_map: dict[str, str],
    normalize_folders: bool,
) -> None:
    """Show what would happen with no server state (every rule treated as new)."""
    click.echo("Offline preview (no token): server state is unknown.\n")
    decisions = [
        ConflictDecision(
            rule, Decision.CREATE, "Offline preview (server state unknown).", folder_missing=True
        )
        for rule in ruleset.rules
    ]
    plan = build_plan(
        decisions,
        excludes=excludes,
        address_map=address_map,
        normalize_folders=normalize_folders,
    )
    _print_plan(plan, existing_rules=[], mailbox=ruleset.source_mailbox)
    click.echo("\nProvide --token (or set MAIL_TOKEN/OWA_TOKEN) to detect conflicts and apply.")


def _render_suggestions(
    suggestions: list[RuleSuggestion], *, sampled: int, folder: str
) -> str:
    lines = [
        f"Sampled {sampled} messages from {folder} (metadata only).",
        f"{len(suggestions)} suggestion(s):",
        "",
    ]
    if not suggestions:
        lines.append("No unfiled patterns found above the current thresholds.")
        return "\n".join(lines)
    for i, s in enumerate(suggestions, 1):
        lines.append(f"{i}. {s.name}   (x{s.message_count}, confidence {s.confidence:.2f})")
        lines.append(f"     match:   {_condition_text(s.condition)}")
        lines.append(f"     move to: {s.suggested_folder}")
        lines.append(f"     why:     {s.reason}")
        if s.examples:
            lines.append(f"     e.g.:    {s.examples[0]}")
        lines.append("")
    lines.append("These are advisory. Apply accepted ones via `apply --from-json`.")
    return "\n".join(lines)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _format_api_error(exc: MailboxApiError) -> str:
    msg = str(exc)
    if exc.status in (401, 403):
        msg += (
            "\nThe token was rejected. It may be expired (mailbox tokens are short-lived, "
            "about an hour) or missing the required scope. "
            "Run `rwz-importer token-help` for how to get a fresh one."
        )
    return msg


_TOKEN_HELP = """\
Obtain a token (Outlook shown below; see other providers)
=========================================================

This covers Outlook/Exchange. For other providers:
  - Gmail: paste an OAuth token from https://developers.google.com/oauthplayground/
    (scopes: gmail.settings.basic gmail.labels gmail.modify), or run
    `rwz-importer login --provider gmail --client-id ... --client-secret ...`.
  - IMAP: no token — set MAIL_IMAP_HOST / MAIL_IMAP_USERNAME / MAIL_IMAP_PASSWORD
    and use `--provider imap`.

Obtain an OWA bearer token
==========================

The token is read at runtime from --token or the MAIL_TOKEN/OWA_TOKEN environment
variable and is NEVER written to disk by this tool. Treat it like a password: it
grants access to your mailbox and expires after about an hour.

Easiest: sign in with Microsoft
-------------------------------
Run:
    rwz-importer login

It prints a short code and a URL; sign in there as usual (MFA included) and the
token is printed back, ready to paste into OWA_TOKEN. No DevTools required.
(The web UI has a "Sign in with Microsoft" button that does the same thing.)

NOTE: some organisations block device-code sign-in via Conditional Access. If
`login` fails with "your sign-in ... does not meet the criteria to access this
resource", that's an admin policy, not a bug -- use the manual method below,
which reuses the browser session your org already trusts.

Manual: copy the token from a signed-in Outlook tab
---------------------------------------------------
1. Sign in to https://outlook.office.com and let the mailbox load.
2. Open DevTools (F12) -> Console tab.
3. Paste the snippet below and press Enter. It finds your Outlook token and
   copies it to the clipboard. (If the console warns about pasting, type
   "allow pasting" first.)

    (() => {
      const aud = (t) => { try { const p = (t.split(".")[1] || ""); return (JSON.parse(atob(p.replace(/-/g, "+").replace(/_/g, "/"))) || {}).aud || ""; } catch (e) { return ""; } };
      const owa = (s) => { s = (s || "").toLowerCase(); return s.indexOf("outlook.office.com") >= 0 || s.indexOf("00000002-0000-0ff1-ce00-000000000000") >= 0; };
      const hits = [];
      const scan = (store) => {
        for (let i = 0; i < store.length; i++) {
          const raw = store.getItem(store.key(i));
          if (!raw) continue;
          let o = null; try { o = JSON.parse(raw); } catch (e) {}
          if (o && o.credentialType === "AccessToken" && typeof o.secret === "string") {
            if (owa(aud(o.secret)) || owa(o.target)) hits.push(o.secret);
          } else if (raw.indexOf("eyJ") === 0 && owa(aud(raw))) {
            hits.push(raw);
          }
        }
      };
      scan(localStorage); scan(sessionStorage);
      if (!hits.length) { console.warn("No Outlook token found. Open outlook.office.com, let it load, then retry."); return; }
      try { copy(hits[0]); console.log("Outlook token copied to clipboard."); } catch (e) {}
      console.log(hits[0]);
    })();

4. Provide it to the tool (PowerShell):
    $env:OWA_TOKEN = "<paste-token-here>"
    rwz-importer apply --file rules.rwz --dry-run

If the snippet finds nothing, fall back to the Network tab: open any request to
outlook.office.com/api/v2.0/me/... and copy the "authorization" header value
after "Bearer ".

Never paste your token into a shared chat, ticket, or screenshot.
"""


if __name__ == "__main__":  # pragma: no cover
    cli()
