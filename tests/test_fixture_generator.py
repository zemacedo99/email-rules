"""The bundled ``sample_rules.rwz`` must be exactly what the synthetic generator emits.

This guards the fixture's provenance: it is built from invented data by
``tests/fixtures/make_sample_rwz.py``, never copied from a real mailbox export.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_fixture_matches_generator(fixtures_dir: Path, rwz_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_sample_rwz", fixtures_dir / "make_sample_rwz.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert rwz_path.read_bytes() == module.build()
