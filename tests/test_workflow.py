import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]


def prepare_resources(service, record_id, reserve_km=18.0, batch_no='B-100', vessel='CS-1', crew='TEAM-A', window_start='2026-10-01T00:00:00Z', window_end='2026-10-03T00:00:00Z'):
    dispatcher = Actor('dispatcher-1', 'dispatcher')
    service.create_batch(dispatcher, batch_no, 50.0)
    draft = service.register_occupation(dispatcher, record_id, {
        'vessel_code': vessel, 'crew_code': crew, 'batch_no': batch_no,
        'reserve_km': reserve_km, 'window_start': window_start, 'window_end': window_end,
    })
    service.confirm_occupation(dispatcher, record_id, draft['version'])


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        self.assertEqual(record["state"], "detected")
        for index, (action, role, data, expected_state) in enumerate(FLOW):
            if action == 'mobilize':
                prepare_resources(self.service, record["id"])
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "noc_operator"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("occupy_confirmed", actions)
        self.assertIn("occupy_consumed", actions)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])
        batch = self.service.get_batch(Actor("creator", "noc_operator"), "B-100")
        # 占用18、实耗16，返还2，最终余量 = 50 - 18 + 2
        self.assertEqual(batch["available_km"], 34.0)

    def test_cancel_before_mobilize_discards_draft(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30002", CREATE_DATA)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})
        self.service.create_batch(Actor("d", "dispatcher"), "B-101", 30.0)
        draft = self.service.register_occupation(Actor("d", "dispatcher"), record["id"], {
            'vessel_code': 'CS-9', 'crew_code': 'TEAM-X', 'batch_no': 'B-101',
            'reserve_km': 18.0, 'window_start': '2026-10-01T00:00:00Z', 'window_end': '2026-10-02T00:00:00Z',
        })
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(self.service.get_batch(Actor("d", "dispatcher"), "B-101")["available_km"], 30.0)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "cancel", {"cancel_reason": "天气恶化"})
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(self.service.get_batch(Actor("d", "dispatcher"), "B-101")["available_km"], 30.0)
        occs = self.service.list_occupations(Actor("d", "dispatcher"))
        self.assertEqual(occs[0]["status"], "discarded")

    def test_cancel_detected_without_occupation(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30003", CREATE_DATA)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "cancel", {"cancel_reason": "误报"})
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(self.service.list_occupations(Actor("d", "dispatcher")), [])
