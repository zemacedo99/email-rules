"""Shared fixtures: paths to the bundled ``.rwz`` fixture and its parsed baseline."""

from __future__ import annotations

from pathlib import Path

import pytest

from rwz_importer.models import RuleSet
from rwz_importer.parser import parse_rwz_file

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def rwz_path() -> Path:
    return FIXTURES / "sample_rules.rwz"


@pytest.fixture
def expected_json_path() -> Path:
    return FIXTURES / "expected_parsed_rules.json"


@pytest.fixture
def parsed_ruleset(rwz_path: Path) -> RuleSet:
    return parse_rwz_file(rwz_path)
