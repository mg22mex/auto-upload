"""Round-robin lead distribution across branch rep rosters."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import (  # noqa: E402
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
from src.odoo_sync.crm import (  # noqa: E402
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

_REP_ENV = (ENV_REPS_PERIFERICO, ENV_REPS_SAN_FELIPE, ENV_DEFAULT_REP_PHONE)


class RepEnvTestCase(unittest.TestCase):
    """Clears rep env and the shared rotation cursor around every test."""

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _REP_ENV:
            os.environ.pop(key, None)
        reset_round_robin()
        self.addCleanup(reset_round_robin)

    @staticmethod
    def assigner(
        *,
        periferico: list[SalesRep] | None = None,
        san_felipe: list[SalesRep] | None = None,
        default: str = "",
    ) -> RoundRobinAssigner:
        table = {
            PRIMARY_BRANCH: periferico or [],
            PLACEHOLDER_BRANCH: san_felipe or [],
        }
        return RoundRobinAssigner(table, default_phone=default)


class TestRosterParsing(RepEnvTestCase):
    def test_parse_reps_normalizes_and_drops_junk(self):
        raw = json.dumps(
            [
                {"odoo_id": 1, "phone": "614 111 1111"},
                {"odoo_id": "2", "phone": "+52 614-222-2222", "name": "Beto"},
                {"nonsense": True},
                "not-a-dict",
            ]
        )

        reps = parse_reps(raw)

        self.assertEqual([r.phone for r in reps], ["+526141111111", "+526142222222"])
        self.assertEqual([r.odoo_id for r in reps], [1, 2])
        self.assertEqual(reps[1].name, "Beto")

    def test_parse_reps_tolerates_invalid_json(self):
        self.assertEqual(parse_reps("{not json"), [])
        self.assertEqual(parse_reps(""), [])
        self.assertEqual(parse_reps(None), [])

    def test_load_branch_reps_reads_both_branches(self):
        os.environ.update(
            REPS_PERIFERICO='[{"odoo_id": 1, "phone": "+526141111111"}]',
            REPS_SAN_FELIPE='[{"odoo_id": 3, "phone": "+526143333333"}]',
        )

        table = load_branch_reps()

        self.assertEqual(table[PRIMARY_BRANCH][0].odoo_id, 1)
        self.assertEqual(table[PLACEHOLDER_BRANCH][0].odoo_id, 3)

    def test_normalize_rep_phone_adds_country_code(self):
        self.assertEqual(normalize_rep_phone("6141234567"), "+526141234567")
        self.assertEqual(normalize_rep_phone("+52 614 123 4567"), "+526141234567")
        self.assertEqual(normalize_rep_phone(""), "")

    def test_default_rep_phone_uses_periferico_head(self):
        os.environ[ENV_REPS_PERIFERICO] = '[{"odoo_id": 1, "phone": "6141111111"}]'
        self.assertEqual(default_rep_phone(), "+526141111111")

        os.environ[ENV_DEFAULT_REP_PHONE] = "+526148888888"
        self.assertEqual(default_rep_phone(), "+526148888888")


class TestRotation(RepEnvTestCase):
    def test_rotation_cycles_and_wraps(self):
        assigner = self.assigner(periferico=PERIFERICO)

        picks = [assigner.next_rep(PRIMARY_BRANCH) for _ in range(5)]

        self.assertEqual([p.odoo_id for p in picks], [1, 2, 1, 2, 1])
        self.assertEqual(
            [p.phone for p in picks][:2], ["+526141111111", "+526142222222"]
        )
        self.assertEqual(picks[0].rotation_index, 0)
        self.assertFalse(picks[0].fell_back)

    def test_branches_rotate_independently(self):
        assigner = self.assigner(periferico=PERIFERICO, san_felipe=SAN_FELIPE)

        first_sf = assigner.next_rep(PLACEHOLDER_BRANCH)
        first_p = assigner.next_rep(PRIMARY_BRANCH)
        second_sf = assigner.next_rep(PLACEHOLDER_BRANCH)

        self.assertEqual(first_sf.odoo_id, 3)
        self.assertEqual(second_sf.odoo_id, 3)
        self.assertEqual(first_p.odoo_id, 1)

    def test_branch_tags_select_roster(self):
        assigner = self.assigner(periferico=PERIFERICO, san_felipe=SAN_FELIPE)

        self.assertEqual(assigner.next_rep(tag="2018 Mercedes +").odoo_id, 3)
        self.assertEqual(assigner.next_rep(tag="2021 Mazda *").odoo_id, 1)
        # Consignment stock is handled by the Periférico desk.
        self.assertEqual(assigner.next_rep(tag="2020 Audi -").odoo_id, 2)

    def test_branch_for_tag_defaults_to_primary(self):
        self.assertEqual(branch_for_tag(None), PRIMARY_BRANCH)
        self.assertEqual(branch_for_tag(""), PRIMARY_BRANCH)
        self.assertEqual(branch_for_tag("+"), PLACEHOLDER_BRANCH)


class TestFallbacks(RepEnvTestCase):
    def test_unmapped_branch_falls_back_to_primary_roster(self):
        pick = self.assigner(periferico=PERIFERICO).next_rep(PLACEHOLDER_BRANCH)

        self.assertEqual(pick.branch, PRIMARY_BRANCH)
        self.assertEqual(pick.odoo_id, 1)
        self.assertTrue(pick.fell_back)

    def test_empty_rosters_fall_back_to_default_phone(self):
        pick = self.assigner(default="+526149999999").next_rep(PLACEHOLDER_BRANCH)

        self.assertEqual(pick.phone, "+526149999999")
        self.assertIsNone(pick.odoo_id)
        self.assertTrue(pick.fell_back)
        self.assertTrue(pick.assigned)

    def test_no_reps_and_no_default_leaves_lead_unassigned(self):
        pick = self.assigner().next_rep(PRIMARY_BRANCH)

        self.assertEqual(pick.phone, "")
        self.assertFalse(pick.assigned)


class TestSharedRotation(RepEnvTestCase):
    def test_module_level_assign_lead_owner_shares_rotation(self):
        os.environ[ENV_REPS_PERIFERICO] = (
            '[{"odoo_id": 1, "phone": "+526141111111"},'
            ' {"odoo_id": 2, "phone": "+526142222222"}]'
        )
        reset_round_robin()

        self.assertEqual(assign_lead_owner(PRIMARY_BRANCH).odoo_id, 1)
        self.assertEqual(assign_lead_owner(PRIMARY_BRANCH).odoo_id, 2)
        reset_round_robin()
        self.assertEqual(assign_lead_owner(PRIMARY_BRANCH).odoo_id, 1)

    def test_roster_change_does_not_break_rotation(self):
        os.environ[ENV_REPS_PERIFERICO] = (
            '[{"odoo_id": 1, "phone": "+526141111111"},'
            ' {"odoo_id": 2, "phone": "+526142222222"}]'
        )
        reset_round_robin()
        self.assertEqual(assign_lead_owner(PRIMARY_BRANCH).odoo_id, 1)
        self.assertEqual(assign_lead_owner(PRIMARY_BRANCH).odoo_id, 2)

        os.environ[ENV_REPS_PERIFERICO] = '[{"odoo_id": 9, "phone": "+526149999999"}]'

        self.assertEqual(assign_lead_owner(PRIMARY_BRANCH).odoo_id, 9)


if __name__ == "__main__":
    unittest.main()
