"""End-to-end regression: parsing the fixture must equal the committed baseline.

This is the safety net for the parser. Any change in extraction behaviour shows up
here as a diff against ``expected_parsed_rules.json``. The volatile ``extracted_at``
timestamp is ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

from rwz_importer.parser import parse_rwz_file


def test_parse_fixture_matches_expected_json(rwz_path: Path, expected_json_path: Path) -> None:
    actual = parse_rwz_file(rwz_path).to_dict()
    expected = json.loads(expected_json_path.read_text(encoding="utf-8"))

    actual.pop("extracted_at", None)
    expected.pop("extracted_at", None)

    assert actual == expected
