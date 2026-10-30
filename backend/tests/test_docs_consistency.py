"""README must use the approved five-state taxonomy from AGENTS.md Rule E.

Regression test for a docs drift where the README defined its own
conflicting state names (supported / partially supported / inconclusive /
missing evidence) and called them provisional.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENTS = (REPO / "AGENTS.md").read_text(encoding="utf-8")
README = (REPO / "README.md").read_text(encoding="utf-8")

APPROVED_STATES = [
    "Verified",
    "Visually supported",
    "Contradicted",
    "Insufficient evidence",
    "Not visually verifiable",
]

# Bolded state names from the drifted README; each must be gone. Checked as
# exact `**...**` tokens so "**Visually supported**" never matches
# "**supported**".
RETIRED_TOKENS = [
    "**supported**",
    "**partially supported**",
    "**inconclusive**",
    "**missing evidence**",
]


def _rule_e_section() -> str:
    start = AGENTS.index("## Rule E")
    end = AGENTS.index("## Rule F")
    return AGENTS[start:end]


def test_rule_e_defines_five_approved_states():
    section = _rule_e_section()
    for state in APPROVED_STATES:
        assert f"**{state}**" in section, f"Rule E no longer defines {state!r}"


def test_readme_uses_approved_taxonomy_terms():
    for state in APPROVED_STATES:
        assert f"**{state}**" in README, f"README missing approved state {state!r}"


def test_readme_has_no_retired_state_names():
    for token in RETIRED_TOKENS:
        assert token not in README, f"README still uses retired state name {token}"


def test_readme_taxonomy_is_not_provisional():
    unstable = re.search(r"working taxonomy|names are finalized|provisional", README, re.IGNORECASE)
    assert unstable is None, f"README suggests the taxonomy is provisional: {unstable.group(0)!r}"
