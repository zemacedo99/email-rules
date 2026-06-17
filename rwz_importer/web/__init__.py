"""A small localhost web UI over the rwz-importer service layer.

:func:`create_app` builds a Starlette app whose JSON API mirrors
:mod:`rwz_importer.service`: inspect/CRUD rules, list folders, suggest rules from
recent mail, run rules over the existing backlog (preview then confirm), and
import a ``.rwz`` through the plan/confirm gate.

The OWA token lives **only** in the server process's memory (seeded from
``OWA_TOKEN`` if set). It is never written to disk, never returned to the browser,
and never placed in a URL. The server binds to loopback only.
"""

from rwz_importer.web.app import create_app, main, run

__all__ = ["create_app", "run", "main"]
