import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src.repository import RECONCILE_OK, RECONCILE_PENDING


DISPATCHER = Actor('dispatcher-1', 'dispatcher')
ADMIN = Actor('root', 'admin')
NOC = Actor('noc-1', 'noc_operator')


def iso_past(days_ago):
    return (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=days_ago)).isoformat()


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.repo = self.service.repository
        self.service.create_batch(DISPATCHER, 'B-OLD', 40.0)

    def tearDown(self):
        self.temp.cleanup()

    def insert_legacy(self, reference, state, payload, created_at=None):
        now = iso_past(3) if created_at is None else created_at
        with self.repo._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (reference, state, 1, json.dumps(payload, ensure_ascii=False), 'legacy', 'legacy', now, now),
            )
            return int(cursor.lastrowid)

    def legacy_payload(self, **overrides):
        payload = {
            'cable': 'SEA-OLD', 'segment': 'X1', 'start_km': 0.0, 'end_km': 15.0,
            'repair_distance_km': 15.0, 'required_spare_km': 15.75,
            'vessel_name': 'CS-OLD', 'estimated_repair_hours': 20.0,
        }
        payload.update(overrides)
        return payload

    def test_migration_backfills_mobilized_and_deducts(self):
        rid = self.insert_legacy('OLD-1', 'mobilized', self.legacy_payload(batch_no='B-OLD'))
        result = self.service.migrate_legacy(ADMIN)
        self.assertEqual(result['backfilled'], [rid])
        occ = self.repo.get_open_occupation(rid)
        self.assertEqual(occ['status'], 'confirmed')
        self.assertEqual(occ['vessel_code'], 'CS-OLD')
        self.assertEqual(occ['reserve_km'], 15.75)
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-OLD')['available_km'], 24.25)
        record = self.repo.get(rid)
        self.assertEqual(record['reconcile_status'], RECONCILE_OK)

    def test_migration_unknown_batch_goes_pending_and_blocks_planning(self):
        rid = self.insert_legacy('OLD-2', 'mobilized', self.legacy_payload(batch_no='B-MISSING'))
        result = self.service.migrate_legacy(ADMIN)
        self.assertEqual(result['pending'], [rid])
        pending = self.service.list_pending(DISPATCHER)
        self.assertEqual([item['id'] for item in pending], [rid])
        occ = self.repo.get_open_occupation(rid)
        self.assertEqual(occ['status'], 'draft')
        self.assertEqual(occ['shortage_km'], 15.75)
        record = self.repo.get(rid)
        self.assertEqual(record['reconcile_status'], RECONCILE_PENDING)
        # 待核对任务不能登记新占用，也不能执行动作
        with self.assertRaises(Conflict):
            self.service.register_occupation(DISPATCHER, rid, {
                'vessel_code': 'CS-X', 'crew_code': 'T-X', 'batch_no': 'B-OLD', 'reserve_km': 16.0,
                'window_start': '2026-10-10T00:00:00Z', 'window_end': '2026-10-11T00:00:00Z',
            })
        with self.assertRaises(Conflict):
            self.service.act(Actor('vm', 'vessel_master'), rid, record['version'], 'return_port', {'reason': 'x'})
        # 待核对任务不参与区段冲突检查
        newer = self.service.create(NOC, 'NEW-1', {
            'cable': 'SEA-OLD', 'segment': 'X1', 'start_km': 2.0, 'end_km': 10.0,
            'depth_m': 100.0, 'sea_state': 2, 'vessel_available': True, 'spare_length_km': 20.0,
            'permit_valid': True, 'capacity_gbps': 100,
        })
        self.assertEqual(newer['state'], 'detected')

    def test_reconcile_releases_pending_and_reopens_planning(self):
        rid = self.insert_legacy('OLD-3', 'mobilized', self.legacy_payload(batch_no='B-MISSING'))
        self.service.migrate_legacy(ADMIN)
        with self.assertRaises(Conflict) as caught:
            self.service.reconcile_occupation(DISPATCHER, rid, {'batch_no': 'B-OLD', 'reserve_km': 50.0})
        self.assertEqual(caught.exception.details['shortage_km'], 10.0)
        occ = self.service.reconcile_occupation(DISPATCHER, rid, {'batch_no': 'B-OLD', 'reserve_km': 15.75})
        self.assertEqual(occ['status'], 'confirmed')
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-OLD')['available_km'], 24.25)
        self.assertEqual(self.repo.get(rid)['reconcile_status'], RECONCILE_OK)
        self.assertEqual(self.service.list_pending(DISPATCHER), [])

    def test_migration_spliced_task_is_consumed_without_deduction(self):
        rid = self.insert_legacy('OLD-4', 'spliced',
                                 self.legacy_payload(batch_no='B-OLD', spare_used_km=16.0))
        result = self.service.migrate_legacy(ADMIN)
        self.assertEqual(result['backfilled'], [rid])
        occs = self.service.list_occupations(DISPATCHER)
        occ = next(item for item in occs if item['record_id'] == rid)
        self.assertEqual(occ['status'], 'consumed')
        self.assertEqual(occ['spare_used_km'], 16.0)
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-OLD')['available_km'], 40.0)

    def test_migration_is_idempotent(self):
        rid = self.insert_legacy('OLD-5', 'mobilized', self.legacy_payload(batch_no='B-OLD'))
        first = self.service.migrate_legacy(ADMIN)
        second = self.service.migrate_legacy(ADMIN)
        self.assertEqual(first['backfilled'], [rid])
        self.assertEqual(second['backfilled'], [])
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-OLD')['available_km'], 24.25)

    def test_migration_requires_admin(self):
        self.insert_legacy('OLD-6', 'mobilized', self.legacy_payload(batch_no='B-OLD'))
        with self.assertRaises(Exception):
            self.service.migrate_legacy(DISPATCHER)
