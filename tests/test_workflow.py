import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]


def window(hours_start=0, hours_end=48):
    base = datetime.now(timezone.utc)
    return (base + timedelta(hours=hours_start)).isoformat(), (base + timedelta(hours=hours_end)).isoformat()


def prepare_occupation(service, record, reserve_km=18.0, vessel='CS-1', crew='CREW-A', batch='BATCH-1'):
    """批准后登记并确认资源占用，返回确认结果。"""
    start, end = window()
    draft = service.register_occupation(Actor('disp1', 'dispatcher'), record['id'], {'vessel_name': vessel, 'splice_crew': crew, 'batch_no': batch, 'reserve_km': reserve_km, 'window_start': start, 'window_end': end})
    return service.confirm_occupation(Actor('disp1', 'dispatcher'), draft['id'])


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))
        self.service.register_spare_batch(Actor('disp1', 'dispatcher'), 'BATCH-1', 100.0)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        self.assertEqual(record["state"], "detected")
        for index, (action, role, data, expected_state) in enumerate(FLOW):
            if action == 'mobilize':
                prepare_occupation(self.service, record)
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "noc_operator"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn(FLOW[-1][0], actions)
        self.assertEqual(actions[-1], 'resource_released')
        self.assertIn('resource_confirmed', actions)
        self.assertIn('resource_released', actions)
        # 任务结束后未消耗备缆退回批次：100 - 18 + (18 - 16)
        batch = self.service.list_spare_batches(Actor("creator", "noc_operator"))[0]
        self.assertEqual(batch["remaining_km"], 84.0)
