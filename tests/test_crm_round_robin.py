"""Round-robin lead distribution across branch rep rosters."""
from __future__ import annotations

import json

import pytest

from src.config import (
    ENV_DEFAULT_REP_PHONE,
    ENV_REPS_PERIFERICO,
    ENV_REPS_SAN_FELIPE,
    SalesRep,
    branch_for_tag,
    default_rep_phone,
    load_branch_reps,
    normalize_rep_phone,
    parse_reps,
)
from src.odoo_sync.crm import (
    PLACEHOLDER_BRANCH,
    PRIMARY_BRANCH,
    RoundRobinAssigner,
    assign_lead_owner,
    reset_round_robin,
)

PERIFERICO = [
    SalesRep(phone="+526141111111", odoo_id=1, name="Ana"),
    SalesRep(phone="+526142222222", odoo_id=2, name="Beto"),
]
SAN_FELIPE = [SalesRep(phone="+526143333333", odoo_id=3, name="Carla")]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (ENV_REPS_PERIFERICO, ENV_REPS_SAN_FELIPE, ENV_DEFAULT_REP_PHONE):
        monkeypatch.delenv(key, raising=False)
    reset_round_robin()
    yield
    reset_round_robin()


def _assigner(**reps) -> RoundRobinAssigner:
    table = {PRIMARY_BRANCH: reps.get("periferico", []), PLACEHOLDER_BRANCH: reps.get("san_felipe", [])}
    return RoundRobinAssigner(table, default_phone=reps.get("default", ""))


def test_parse_reps_normalizes_and_drops_junk():
    raw = json.dumps(
        [
            {"odoo_id": 1, "phone": "614 111 1111"},
            {"odoo_id": "2", "phone": "+52 614-222-2222", "name": "Beto"},
            {"nonsense": True},
            "not-a-dict",
        ]
    )

    reps = parse_reps(raw)

    assert [r.phone for r in reps] == ["+526141111111", "+526142222222"]
    assert [r.odoo_id for r in reps] == [1, 2]
    assert reps[1].name == "Beto"


def test_parse_reps_tolerates_invalid_json():
    assert parse_reps("{not json") == []
    assert parse_reps("") == []
    assert parse_reps(None) == []


def test_load_branch_reps_reads_both_branches(monkeypatch):
    monkeypatch.setenv(ENV_REPS_PERIFERICO, '[{"odoo_id": 1, "phone": "+526141111111"}]')
    monkeypatch.setenv(ENV_REPS_SAN_FELIPE, '[{"odoo_id": 3, "phone": "+526143333333"}]')

    table = load_branch_reps()

    assert table[PRIMARY_BRANCH][0].odoo_id == 1
    assert table[PLACEHOLDER_BRANCH][0].odoo_id == 3


def test_rotation_cycles_and_wraps():
    assigner = _assigner(periferico=PERIFERICO)

    picks = [assigner.next_rep(PRIMARY_BRANCH) for _ in range(5)]

    assert [p.odoo_id for p in picks] == [1, 2, 1, 2, 1]
    assert [p.phone for p in picks][:2] == ["+526141111111", "+526142222222"]
    assert picks[0].rotation_index == 0
    assert not picks[0].fell_back


def test_branches_rotate_independently():
    assigner = _assigner(periferico=PERIFERICO, san_felipe=SAN_FELIPE)

    first_sf = assigner.next_rep(PLACEHOLDER_BRANCH)
    first_p = assigner.next_rep(PRIMARY_BRANCH)
    second_sf = assigner.next_rep(PLACEHOLDER_BRANCH)

    assert first_sf.odoo_id == 3
    assert second_sf.odoo_id == 3
    assert first_p.odoo_id == 1


def test_branch_tags_select_roster():
    assigner = _assigner(periferico=PERIFERICO, san_felipe=SAN_FELIPE)

    assert assigner.next_rep(tag="2018 Mercedes +").odoo_id == 3
    assert assigner.next_rep(tag="2021 Mazda *").odoo_id == 1
    # Consignment stock is handled by the Periférico desk.
    assert assigner.next_rep(tag="2020 Audi -").odoo_id == 2


def test_branch_for_tag_defaults_to_primary():
    assert branch_for_tag(None) == PRIMARY_BRANCH
    assert branch_for_tag("") == PRIMARY_BRANCH
    assert branch_for_tag("+") == PLACEHOLDER_BRANCH


def test_unmapped_branch_falls_back_to_primary_roster():
    assigner = _assigner(periferico=PERIFERICO)

    pick = assigner.next_rep(PLACEHOLDER_BRANCH)

    assert pick.branch == PRIMARY_BRANCH
    assert pick.odoo_id == 1
    assert pick.fell_back


def test_empty_rosters_fall_back_to_default_phone():
    assigner = _assigner(default="+526149999999")

    pick = assigner.next_rep(PLACEHOLDER_BRANCH)

    assert pick.phone == "+526149999999"
    assert pick.odoo_id is None
    assert pick.fell_back
    assert pick.assigned


def test_no_reps_and_no_default_leaves_lead_unassigned():
    pick = _assigner().next_rep(PRIMARY_BRANCH)

    assert pick.phone == ""
    assert not pick.assigned


def test_default_rep_phone_uses_periferico_head(monkeypatch):
    monkeypatch.setenv(ENV_REPS_PERIFERICO, '[{"odoo_id": 1, "phone": "6141111111"}]')

    assert default_rep_phone() == "+526141111111"

    monkeypatch.setenv(ENV_DEFAULT_REP_PHONE, "+526148888888")
    assert default_rep_phone() == "+526148888888"


def test_normalize_rep_phone_adds_country_code():
    assert normalize_rep_phone("6141234567") == "+526141234567"
    assert normalize_rep_phone("+52 614 123 4567") == "+526141234567"
    assert normalize_rep_phone("") == ""


def test_module_level_assign_lead_owner_shares_rotation(monkeypatch):
    monkeypatch.setenv(
        ENV_REPS_PERIFERICO,
        '[{"odoo_id": 1, "phone": "+526141111111"},'
        ' {"odoo_id": 2, "phone": "+526142222222"}]',
    )
    reset_round_robin()

    assert assign_lead_owner(PRIMARY_BRANCH).odoo_id == 1
    assert assign_lead_owner(PRIMARY_BRANCH).odoo_id == 2
    reset_round_robin()
    assert assign_lead_owner(PRIMARY_BRANCH).odoo_id == 1


def test_roster_change_does_not_break_rotation(monkeypatch):
    monkeypatch.setenv(
        ENV_REPS_PERIFERICO,
        '[{"odoo_id": 1, "phone": "+526141111111"},'
        ' {"odoo_id": 2, "phone": "+526142222222"}]',
    )
    reset_round_robin()
    assert assign_lead_owner(PRIMARY_BRANCH).odoo_id == 1
    assert assign_lead_owner(PRIMARY_BRANCH).odoo_id == 2

    monkeypatch.setenv(ENV_REPS_PERIFERICO, '[{"odoo_id": 9, "phone": "+526149999999"}]')

    assert assign_lead_owner(PRIMARY_BRANCH).odoo_id == 9
