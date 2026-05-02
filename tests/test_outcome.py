"""Tests for the decision-outcome normaliser."""
from __future__ import annotations

import pytest

from acp_decisions.outcome import normalise_outcome


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Grant variants
        ("Grant permission with conditions", "granted_with_conditions"),
        ("Grant permission with revised conditions", "granted_with_conditions"),
        ("Grant Permissions with Conditions", "granted_with_conditions"),
        ("Grant Perm. w   Conditions", "granted_with_conditions"),
        ("Grant Perm. w Conditions", "granted_with_conditions"),
        ("Grant permission", "granted"),
        ("Grant", "granted"),
        # Refuse variants
        ("Refuse Permission", "refused"),
        ("Refuse permission", "refused"),
        ("Refuse Perm.", "refused"),
        ("1st Refuse permission", "refused"),
        # Other
        ("Withdrawn", "withdrawn"),
        ("Invalid", "invalid"),
        ("Contribution Appeal Decided", "procedural"),
    ],
)
def test_normalise_outcome(raw: str, expected: str) -> None:
    assert normalise_outcome(raw) == expected


def test_normalise_outcome_unknown_returns_procedural() -> None:
    """Unknown free-text strings fall into the catch-all 'procedural' bucket."""
    assert normalise_outcome("Some weird unhandled outcome string") == "procedural"


def test_normalise_outcome_handles_empty() -> None:
    assert normalise_outcome("") == "procedural"
