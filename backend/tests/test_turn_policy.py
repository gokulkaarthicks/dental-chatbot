"""Turn policy: which tools one model round may see, with no DB, LLM or graph.

These tests pin the rules that decide tool visibility: a natural-language re-ask
may search again but must never reach a booking or confirmation tool, a family
request must not collapse into a solo search, and an exhausted search withdraws
everything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent.tools import (
    ALL_TOOLS,
    CONFIRM_TOOL_NAMES,
    PUBLIC_TOOL_NAMES,
    REASK_TOOL_NAMES,
    REGISTRATION_GATED_TOOL_NAMES,
    SOLO_SEARCH_TOOL,
    TOOL_BY_NAME,
    TurnMode,
    is_explicit_affirmation,
    visible_tools,
)


def _names(tools) -> set[str]:  # noqa: ANN001
    return {tool.name for tool in tools}


def _reask(**overrides):  # noqa: ANN003, ANN202
    kwargs = {"mode": TurnMode.REASK, "searched": False, "exhausted": False, "household": False}
    kwargs.update(overrides)
    return visible_tools(**kwargs)


def _default(**overrides):  # noqa: ANN003, ANN202
    kwargs = {"mode": TurnMode.DEFAULT, "searched": False, "exhausted": False, "household": False}
    kwargs.update(overrides)
    return visible_tools(**kwargs)


@pytest.fixture(scope="module")
def every_name() -> set[str]:
    return _names(ALL_TOOLS)


# --- what the re-ask policy allows ---------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "create_booking_proposal",
        "confirm_booking_proposal",
        "confirm_slot_selection",
        "confirm_appointment_change",
        "decline_booking_proposal",
        "hold_family_option",
        "reschedule_appointment",
        "cancel_appointment",
    ],
)
def test_reask_policy_excludes_booking_and_confirmation_tools(
    forbidden: str, every_name: set[str]
) -> None:
    assert forbidden in every_name
    assert forbidden not in _names(_reask())


def test_reask_policy_allows_exactly_its_declared_set() -> None:
    assert _names(_reask()) == set(REASK_TOOL_NAMES)


def test_reask_policy_keeps_child_registration_visible() -> None:
    # A family re-ask may name a child who is not linked yet.
    assert "register_child_for_verified_parent" in _names(_reask())


# --- family requests must not collapse to a solo search -------------------


def test_single_participant_request_keeps_solo_search() -> None:
    assert SOLO_SEARCH_TOOL in _names(_reask(household=False))


def test_multi_participant_request_drops_solo_search() -> None:
    assert SOLO_SEARCH_TOOL not in _names(_reask(household=True))


def test_family_request_keeps_family_search() -> None:
    # Only the solo search is withdrawn; the family search is the correct tool.
    assert "search_family_availability" in _names(_reask(household=True))


# --- the default policy is a no-op ---------------------------------------


def test_default_policy_returns_every_tool() -> None:
    assert (
        visible_tools(mode=TurnMode.DEFAULT, searched=False, exhausted=False, household=False)
        == ALL_TOOLS
    )


def test_default_policy_keeps_solo_search_on_a_household() -> None:
    # Only a re-ask is constrained; an ordinary turn may still search solo.
    tools = visible_tools(mode=TurnMode.DEFAULT, searched=False, exhausted=False, household=True)

    assert SOLO_SEARCH_TOOL in _names(tools)


# --- search-aware tool withdrawal ----------------------------------------


def test_reask_withdraws_all_tools_after_a_search() -> None:
    assert _reask(searched=True) == ()


def test_reask_offers_tools_before_a_search() -> None:
    assert _names(_reask()) == set(REASK_TOOL_NAMES)


def test_exhausted_search_withdraws_tools_for_the_default_policy() -> None:
    assert (
        visible_tools(mode=TurnMode.DEFAULT, searched=True, exhausted=True, household=False) == ()
    )


def test_default_policy_does_not_withdraw_after_a_search() -> None:
    tools = visible_tools(mode=TurnMode.DEFAULT, searched=True, exhausted=False, household=False)

    assert len(tools) == len(ALL_TOOLS)


# --- the surface follows what the session can actually do -----------------


def test_an_unverified_turn_sees_exactly_the_public_surface() -> None:
    # Everything else reads or changes one patient's chart, so offering it would
    # only buy a refusal.
    assert _names(_default(verified=False)) == set(PUBLIC_TOOL_NAMES)


def test_a_verified_turn_gets_the_whole_surface_back() -> None:
    assert _names(_default(verified=True)) == _names(ALL_TOOLS)


@pytest.mark.parametrize("gated", sorted(REGISTRATION_GATED_TOOL_NAMES))
def test_an_incomplete_registration_does_not_get_the_search_tools(gated: str) -> None:
    names = _names(_default(registration_complete=False))

    assert gated not in names
    # Reading the chart and asking for a human still work.
    assert "get_patient_appointments" in names
    assert "create_staff_request" in names


def test_a_complete_registration_gets_the_gated_tools_back() -> None:
    assert REGISTRATION_GATED_TOOL_NAMES <= _names(_default())


@pytest.mark.parametrize("confirm", sorted(CONFIRM_TOOL_NAMES))
def test_a_turn_without_the_patients_yes_cannot_spend_a_confirmation(confirm: str) -> None:
    names = _names(_default(may_confirm=False))

    assert confirm not in names
    # Preparing a choice and turning it down are both still allowed.
    assert "create_booking_proposal" in names
    assert "decline_booking_proposal" in names


def test_the_patients_own_yes_unlocks_the_confirmation_tools() -> None:
    assert CONFIRM_TOOL_NAMES <= _names(_default(may_confirm=True))


def test_a_reask_carries_no_confirmation_authority_of_its_own() -> None:
    # The re-ask surface names no confirm tool, whatever the rest of the state.
    assert CONFIRM_TOOL_NAMES.isdisjoint(_names(_reask(may_confirm=True)))


def test_an_unverified_reask_keeps_only_what_it_can_use() -> None:
    assert _names(_reask(verified=False)) <= set(PUBLIC_TOOL_NAMES)


# --- whose agreement counts ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", True),
        ("Yes, book it", True),
        ("that works for me", True),
        ("sounds good", True),
        ("go ahead", True),
        ("I'll take it", True),
        ("confirmed", True),
        ("please do", True),
        ("no thanks", False),
        ("I'm not sure that works", False),
        ("don't book it", False),
        ("can you check other times?", False),
        ("Please hold the selected family option.", False),
        ("what would that cost?", False),
        ("", False),
    ],
)
def test_affirmation_is_read_from_the_patients_own_words(text: str, expected: bool) -> None:
    assert is_explicit_affirmation(text) is expected


def test_an_agreement_after_a_clause_break_still_counts() -> None:
    # A hedge in the first clause is not a refusal of the second one.
    assert is_explicit_affirmation("I was not sure, but yes go ahead") is True


# --- registry basics ------------------------------------------------------


def test_the_registry_is_a_one_to_one_name_index() -> None:
    # A duplicate name would make a tool call ambiguous, so the registry index
    # must have exactly one entry per tool.
    assert len(TOOL_BY_NAME) == len(ALL_TOOLS)
    assert set(TOOL_BY_NAME) == _names(ALL_TOOLS)
    assert TOOL_BY_NAME["search_availability"] in ALL_TOOLS


def test_every_tool_declares_a_model_facing_schema_and_a_description() -> None:
    for tool in ALL_TOOLS:
        assert tool.name
        assert tool.description, f"{tool.name} has no description for the model"
        assert tool.args_schema is not None, f"{tool.name} has no argument schema"


def test_the_tool_package_does_not_import_the_graph_or_the_runner() -> None:
    # The dependency arrow points from the graph into the tool package only.
    import app.agent.tools as tools_package

    package_root = Path(tools_package.__file__).parent
    forbidden_prefixes = ("app.agent.graph", "app.agent.runner")
    offenders: list[str] = []

    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        imported = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        offenders.extend(
            f"{path.name}: {module}"
            for module in imported
            if module.startswith(forbidden_prefixes)
        )

    assert offenders == []
