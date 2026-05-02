"""Normalise free-text decision-outcome strings into canonical buckets.

ACP records the operative decision as free text on the case page, with
real-world variants like:

    "Grant permission with conditions"
    "Grant Perm. w   Conditions"
    "1st Refuse permission"
    "Refuse Perm."
    "Contribution Appeal Decided"
    "Invalid"

The analytics layer queries on a normalised key, not free text — so each
incoming string maps to one of:

    granted | granted_with_conditions | refused | withdrawn | invalid | procedural

Anything we don't recognise lands in 'procedural' — it's the safe fallback for
ACP's many odd outcome strings (deferred, set aside, contribution appeals, …)
none of which belong in the headline grant/refuse analytics.
"""
from __future__ import annotations


def normalise_outcome(raw: str) -> str:
    """Map a free-text decision string to a canonical outcome bucket."""
    s = raw.strip().lower()
    if not s:
        return "procedural"
    if "invalid" in s:
        return "invalid"
    if "withdraw" in s:
        return "withdrawn"
    if "refuse" in s:
        return "refused"
    if "grant" in s:
        if "condition" in s:
            return "granted_with_conditions"
        return "granted"
    return "procedural"
