import threading
import unittest
from datetime import datetime, timedelta, timezone

from app import build_service
from src.domain import Actor, Conflict, NotFound
from tests.test_workflow import CREATE_DATA, prepare_resources

import tempfile
from pathlib import Path


DISPATCHER = Actor('dispatcher-1', 'dispatcher')


def iso(hours_from_now):
    return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours_from_now)).isoformat()


def approved(service, reference, cable='SEA-1', segment='S3', spare=20.0):
    data = dict(CREATE_DATA)
    data['cable'] = cable
    data['segment'] = segment
    data['spare_length_km'] = spare
    record = service.create(Actor('creator', 'noc_operator'), reference, data)
    return service.act(Actor('rm', 'repair_manager'), record['id'], record['version'], 'approve', {'repair_manager': 'RM-2'})


class OccupationLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.service.create_batch(DISPATCHER, 'B-200', 100.0)

    def tearDown(self):
        self.temp.cleanup()

    def register(self, record_id, reserve, vessel='CS-1', crew='TEAM-A', batch='B-200', start=None, end=None):
        return self.service.register_occupation(DISPATCHER, record_id, {
            'vessel_code': vessel, 'crew_code': crew, 'batch_no': batch, 'reserve_km': reserve,
            'window_start': start or iso(24), 'window_end': end or iso(48),
        })

    def get_open_occupation_for(self, record_id):
        return self.service.repository.get_open_occupation(record_id)

    def test_shortage_keeps_draft_with_gap(self):
        record = approved(self.service, 'CABLE-40001', segment='S3')
        draft = self.register(record['id'], 105.0)
        self.assertEqual(draft['status'], 'draft')
        self.assertEqual(draft['shortage_km'], 5.0)
        self.assertEqual(draft['batch_available_km'], 100.0)
        with self.assertRaises(Conflict) as caught:
            self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'])
        self.assertEqual(caught.exception.details['shortage_km'], 5.0)
        self.assertEqual(caught.exception.details['batch_available_km'], 100.0)
        # 确认失败不扣减余量，草稿仍保留
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 100.0)
        self.assertEqual(self.get_open_occupation_for(record['id'])['status'], 'draft')

    def test_vessel_and_crew_cannot_overlap_same_window(self):
        r1 = approved(self.service, 'CABLE-40002', segment='S4')
        d1 = self.register(r1['id'], 16.0, vessel='CS-1', crew='TEAM-A')
        self.service.confirm_occupation(DISPATCHER, r1['id'], d1['version'])
        r2 = approved(self.service, 'CABLE-40003', segment='S5')
        d2 = self.register(r2['id'], 16.0, vessel='CS-1', crew='TEAM-B')
        self.assertEqual(len(d2['window_conflicts']), 1)
        with self.assertRaises(Conflict) as caught:
            self.service.confirm_occupation(DISPATCHER, r2['id'], d2['version'])
        self.assertEqual(len(caught.exception.details['conflicts']), 1)
        # 班组相同也不行
        d2b = self.register(r2['id'], 16.0, vessel='CS-2', crew='TEAM-A')
        with self.assertRaises(Conflict):
            self.service.confirm_occupation(DISPATCHER, r2['id'], d2b['version'])
        # 换船机、换班组、错开时间窗后可以确认
        d2c = self.register(r2['id'], 16.0, vessel='CS-2', crew='TEAM-B', start=iso(72), end=iso(96))
        self.assertEqual(d2c['window_conflicts'], [])
        confirmed = self.service.confirm_occupation(DISPATCHER, r2['id'], d2c['version'])
        self.assertEqual(confirmed['status'], 'confirmed')

    def test_concurrent_confirm_only_one_succeeds(self):
        # 两票抢同一批备缆：余量100，各要80 => 只有一票成功，失败方看到最新余量20
        r3 = approved(self.service, 'CABLE-40006', segment='S8')
        d3 = self.register(r3['id'], 80.0, vessel='CS-3', crew='TEAM-C', start=iso(24), end=iso(48))
        r4 = approved(self.service, 'CABLE-40007', segment='S9')
        d4 = self.register(r4['id'], 80.0, vessel='CS-4', crew='TEAM-D', start=iso(24), end=iso(48))

        results = {}
        loser_ref = {}

        def attempt(key, rid, version):
            try:
                self.service.confirm_occupation(DISPATCHER, rid, version)
                results[key] = ('ok', None)
            except Conflict as exc:
                results[key] = ('conflict', getattr(exc, 'details', None))
                loser_ref[key] = rid

        t1 = threading.Thread(target=attempt, args=('a', r3['id'], d3['version']))
        t2 = threading.Thread(target=attempt, args=('b', r4['id'], d4['version']))
        t1.start(); t2.start(); t1.join(); t2.join()
        outcomes = list(results.values())
        self.assertEqual(sorted(item[0] for item in outcomes), ['conflict', 'ok'])
        loser_details = next(item[1] for item in outcomes if item[0] == 'conflict')
        self.assertEqual(loser_details['shortage_km'], 60.0)
        self.assertEqual(loser_details['batch_available_km'], 20.0)
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 20.0)
        # 失败方的草稿仍然保留
        loser_rid = next(iter(loser_ref.values()))
        self.assertEqual(self.get_open_occupation_for(loser_rid)['status'], 'draft')

    def test_cancel_after_mobilize_restores_full_reserve(self):
        record = approved(self.service, 'CABLE-40008', segment='S10')
        draft = self.register(record['id'], 16.0)
        self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'])
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'vessel_name': 'CS-1'})
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 84.0)
        record = self.service.act(Actor('rm', 'repair_manager'), record['id'], record['version'], 'cancel',
                                  {'cancel_reason': '许可撤销'})
        self.assertEqual(record['state'], 'cancelled')
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 100.0)
        occ = self.service.repository.get_open_occupation(record['id'])
        self.assertIsNone(occ)

    def test_return_port_releases_resources(self):
        record = approved(self.service, 'CABLE-40009', segment='S11')
        draft = self.register(record['id'], 16.0)
        self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'])
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'vessel_name': 'CS-1'})
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'survey',
                                  {'survey_complete': True, 'fault_location_km': 128})
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'return_port',
                                  {'reason': '台风预警'})
        self.assertEqual(record['state'], 'returned')
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 100.0)
        # 释放后的船机可以再次安排
        later = approved(self.service, 'CABLE-40010', segment='S12')
        d = self.register(later['id'], 16.0)
        self.assertEqual(d['window_conflicts'], [])
        self.service.confirm_occupation(DISPATCHER, later['id'], d['version'])

    def test_splice_fail_releases_resources(self):
        record = approved(self.service, 'CABLE-40011', segment='S13')
        draft = self.register(record['id'], 17.0)
        self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'])
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'vessel_name': 'CS-1'})
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'survey',
                                  {'survey_complete': True, 'fault_location_km': 128})
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'splice_fail',
                                  {'reason': '接头盒进水'})
        self.assertEqual(record['state'], 'splice_failed')
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 100.0)

    def test_splice_settles_actual_consumption(self):
        record = approved(self.service, 'CABLE-40012', segment='S14')
        draft = self.register(record['id'], 18.0)
        self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'])
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'vessel_name': 'CS-1'})
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'survey',
                                  {'survey_complete': True, 'fault_location_km': 128})
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'splice',
                                  {'splice_loss_db': 0.1, 'spare_used_km': 16})
        self.assertEqual(record['state'], 'spliced')
        # 100 - 18 + (18 - 16) = 84
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 84.0)
        with self.assertRaises(Conflict):
            self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'splice',
                             {'splice_loss_db': 0.1, 'spare_used_km': 16})

    def test_stale_draft_version_fails_with_latest(self):
        record = approved(self.service, 'CABLE-40013', segment='S15')
        draft = self.register(record['id'], 16.0)
        updated = self.register(record['id'], 17.0)
        self.assertEqual(updated['version'], draft['version'] + 1)
        with self.assertRaises(Conflict) as caught:
            self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'])
        self.assertEqual(caught.exception.details['latest_version'], updated['version'])

    def test_replenished_batch_allows_confirm(self):
        record = approved(self.service, 'CABLE-40014', segment='S16')
        draft = self.register(record['id'], 105.0)
        with self.assertRaises(Conflict):
            self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'])
        self.service.restock_batch(DISPATCHER, 'B-200', 10.0)
        self.register(record['id'], 105.0)
        confirmed = self.service.confirm_occupation(DISPATCHER, record['id'], draft['version'] + 1)
        self.assertEqual(confirmed['status'], 'confirmed')
        self.assertEqual(self.service.get_batch(DISPATCHER, 'B-200')['available_km'], 5.0)

    def test_unknown_batch_rejected(self):
        record = approved(self.service, 'CABLE-40015', segment='S17')
        with self.assertRaises(NotFound):
            self.register(record['id'], 16.0, batch='NOPE')
