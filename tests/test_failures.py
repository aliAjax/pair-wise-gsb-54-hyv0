import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ResourceConflict


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]


def window(start_hours=0, end_hours=48):
    base = datetime.now(timezone.utc)
    return (base + timedelta(hours=start_hours)).isoformat(), (base + timedelta(hours=end_hours)).isoformat()


def occupation_payload(**overrides):
    start, end = window(overrides.pop('start_hours', 0), overrides.pop('end_hours', 48))
    payload = {'vessel_name': 'CS-1', 'splice_crew': 'CREW-A', 'batch_no': 'BATCH-1', 'reserve_km': 18.0, 'window_start': start, 'window_end': end}
    payload.update(overrides)
    return payload


def make_approved_task(service, reference, create_data=None):
    record = service.create(Actor('creator', 'noc_operator'), reference, create_data or dict(CREATE_DATA))
    return service.act(Actor('rm', 'repair_manager'), record['id'], record['version'], 'approve', {'repair_manager': 'RM-2'})


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))
        self.service.register_spare_batch(Actor('disp1', 'dispatcher'), 'BATCH-1', 100.0)

    def tearDown(self):
        self.temp.cleanup()

    def _draft(self, record, **overrides):
        return self.service.register_occupation(Actor('disp1', 'dispatcher'), record['id'], occupation_payload(**overrides))

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "CABLE-30001", CREATE_DATA)
        self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = make_approved_task(self.service, 'CABLE-30001')
        self.service.confirm_occupation(Actor('disp1', 'dispatcher'), self._draft(record)['id'])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])

    def test_mobilize_without_confirmed_occupation(self):
        record = make_approved_task(self.service, 'CABLE-30001')
        with self.assertRaises(Conflict):
            self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize', FLOW[1][2])

    def test_mobilize_vessel_must_match_occupation(self):
        record = make_approved_task(self.service, 'CABLE-30001')
        self.service.confirm_occupation(Actor('disp1', 'dispatcher'), self._draft(record)['id'])
        data = dict(FLOW[1][2])
        data['vessel_name'] = 'CS-OTHER'
        with self.assertRaises(Conflict):
            self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize', data)

    def test_vessel_double_booking_rejected(self):
        first = make_approved_task(self.service, 'CABLE-30001')
        second_data = dict(CREATE_DATA)
        second_data['segment'] = 'S9'
        second_data['start_km'] = 200.0
        second_data['end_km'] = 210.0
        second = make_approved_task(self.service, 'CABLE-30002', second_data)
        self.service.confirm_occupation(Actor('disp1', 'dispatcher'), self._draft(first)['id'])
        draft2 = self._draft(second, splice_crew='CREW-B')
        with self.assertRaises(ResourceConflict) as caught:
            self.service.confirm_occupation(Actor('disp2', 'dispatcher'), draft2['id'])
        self.assertEqual(caught.exception.context['resource_type'], 'vessel')

    def test_crew_double_booking_rejected_in_non_overlapping_vessels(self):
        first = make_approved_task(self.service, 'CABLE-30001')
        second_data = dict(CREATE_DATA)
        second_data['segment'] = 'S9'
        second_data['start_km'] = 200.0
        second_data['end_km'] = 210.0
        second = make_approved_task(self.service, 'CABLE-30002', second_data)
        self.service.confirm_occupation(Actor('disp1', 'dispatcher'), self._draft(first)['id'])
        draft2 = self._draft(second, vessel_name='CS-2')
        with self.assertRaises(ResourceConflict):
            self.service.confirm_occupation(Actor('disp2', 'dispatcher'), draft2['id'])

    def test_disjoint_windows_allowed(self):
        first = make_approved_task(self.service, 'CABLE-30001')
        second_data = dict(CREATE_DATA)
        second_data['segment'] = 'S9'
        second_data['start_km'] = 200.0
        second_data['end_km'] = 210.0
        second = make_approved_task(self.service, 'CABLE-30002', second_data)
        self.service.confirm_occupation(Actor('disp1', 'dispatcher'), self._draft(first)['id'])
        draft2 = self._draft(second, start_hours=100, end_hours=148)
        confirmed = self.service.confirm_occupation(Actor('disp2', 'dispatcher'), draft2['id'])
        self.assertEqual(confirmed['status'], 'active')
