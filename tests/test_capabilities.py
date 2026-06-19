"""Provider-capability generalization (Phase A).

Covers the new, provider-neutral seam: the ``archive`` action field, capability
gating in the service, and the capability-aware confirmation token. The OWA path
must behave exactly as before (archive implied, ordered by sequence); a Gmail-like
capability set must change those behaviours.
"""

from __future__ import annotations

from rwz_importer import service
from rwz_importer.api.owa_client import ExistingRule, Folder
from rwz_importer.api.provider import ProviderCapabilities
from rwz_importer.models import Action

from tests.test_service import FakeMailbox, _rule, _ruleset

# A Gmail-shaped capability set: unordered filters, no stop, labels, archive is a
# separate choice, flat label namespace.
GMAIL_CAPS = ProviderCapabilities(
    ordered_rules=False,
    stop_processing=False,
    destination_kind="label",
    archive_toggle=True,
    hierarchical_folders=False,
)


def test_action_archive_round_trips() -> None:
    assert Action().archive is True  # default: a parsed move stays a faithful move
    assert Action.from_dict({}).archive is True  # missing key tolerated
    assert Action.from_dict({"archive": False}).archive is False
    assert Action(archive=False).to_dict()["archive"] is False


def test_create_rule_threads_archive_to_provider() -> None:
    mailbox = FakeMailbox(capabilities=GMAIL_CAPS)
    out = service.create_rule(
        mailbox, name="News", move_to_folder="Newsletters", archive=False
    )
    assert out["created"]["archive"] is False
    assert mailbox.created_rules[-1]["archive"] is False


def test_plan_view_exposes_capabilities() -> None:
    view = service.compute_plan(
        FakeMailbox(capabilities=GMAIL_CAPS), _ruleset(_rule("News", "Newsletters"))
    )
    assert view["capabilities"]["destination_kind"] == "label"
    assert view["capabilities"]["archive_toggle"] is True


def _ruleset_with_archive(archive: bool):
    rule = _rule("News", "Newsletters")
    rule.actions.archive = archive
    return _ruleset(rule)


def test_archive_toggle_changes_token_only_when_supported() -> None:
    # Gmail-like (archive is a real, separate choice): the token must distinguish them.
    g_on = service.compute_plan(
        FakeMailbox(capabilities=GMAIL_CAPS), _ruleset_with_archive(True)
    )["confirmation_token"]
    g_off = service.compute_plan(
        FakeMailbox(capabilities=GMAIL_CAPS), _ruleset_with_archive(False)
    )["confirmation_token"]
    assert g_on != g_off

    # Exchange baseline (archive implied by a move): the token is unchanged, so old
    # tokens stay byte-stable and archive can't accidentally gate an apply.
    e_on = service.compute_plan(FakeMailbox(), _ruleset_with_archive(True))[
        "confirmation_token"
    ]
    e_off = service.compute_plan(FakeMailbox(), _ruleset_with_archive(False))[
        "confirmation_token"
    ]
    assert e_on == e_off


def test_update_rule_preserves_archive_when_not_given() -> None:
    """Editing a rule without sending ``archive`` must not silently re-archive it.

    The web edit form doesn't surface the archive choice, so an omitted ``archive``
    has to inherit the existing rule's value (only meaningful on Gmail-like backends).
    """
    existing = ExistingRule(
        id="r1", display_name="News", sequence=1, move_to_folder_id="f1", archive=False
    )
    mailbox = FakeMailbox(
        rules=[existing],
        folders=[Folder(id="f1", display_name="Newsletters")],
        capabilities=GMAIL_CAPS,
    )
    out = service.update_rule(
        mailbox, "r1", name="News", move_to_folder="Newsletters"
    )  # archive omitted
    assert out["updated"]["archive"] is False  # inherited, not reset to True

    # An explicit archive value still wins.
    out2 = service.update_rule(
        mailbox, out["updated"]["id"], name="News", move_to_folder="Newsletters", archive=True
    )
    assert out2["updated"]["archive"] is True


def test_rule_order_is_gated_by_ordered_capability() -> None:
    rules = [
        ExistingRule(id="x", display_name="X", sequence=5),
        ExistingRule(id="y", display_name="Y", sequence=1),
    ]
    # Ordered (Exchange default): sorted by sequence → Y(1) before X(5).
    ordered = service.list_rules(FakeMailbox(rules=list(rules)))
    assert [r["name"] for r in ordered] == ["Y", "X"]

    # Unordered (Gmail): the provider's own order is preserved → X before Y.
    unordered = service.list_rules(FakeMailbox(rules=list(rules), capabilities=GMAIL_CAPS))
    assert [r["name"] for r in unordered] == ["X", "Y"]
