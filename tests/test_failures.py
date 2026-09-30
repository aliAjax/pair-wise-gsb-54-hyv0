import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from tests.test_workflow import CREATE_DATA, FLOW, prepare_resources


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "CABLE-30001", CREATE_DATA)
        self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        prepare_resources(self.service, record["id"])
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])

    def test_mobilize_requires_confirmed_occupation(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30002", CREATE_DATA)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})
        with self.assertRaises(Conflict):
            self.service.act(Actor("vm", "vessel_master"), record["id"], record["version"], "mobilize",
                             {"weather_window_hours": 40, "vessel_name": "CS-1"})

    def test_mobilize_vessel_must_match_occupation(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30003", CREATE_DATA)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})
        prepare_resources(self.service, record["id"], vessel="CS-1")
        with self.assertRaises(Conflict):
            self.service.act(Actor("vm", "vessel_master"), record["id"], record["version"], "mobilize",
                             {"weather_window_hours": 40, "vessel_name": "CS-OTHER"})
