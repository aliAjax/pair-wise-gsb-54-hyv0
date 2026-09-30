import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ResourceConflict
from src.repository import Repository


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
DISPATCHER = Actor('disp1', 'dispatcher')


def window(start_hours=0, end_hours=48):
    base = datetime.now(timezone.utc)
    return (base + timedelta(hours=start_hours)).isoformat(), (base + timedelta(hours=end_hours)).isoformat()


def occupation_payload(**overrides):
    start, end = window(overrides.pop('start_hours', 0), overrides.pop('end_hours', 48))
    payload = {'vessel_name': 'CS-1', 'splice_crew': 'CREW-A', 'batch_no': 'BATCH-1', 'reserve_km': 18.0, 'window_start': start, 'window_end': end}
    payload.update(overrides)
    return payload


class ResourceLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / 'test.db')
        self.service = build_service(self.db_path)
        self.service.register_spare_batch(DISPATCHER, 'BATCH-1', 20.0)

    def tearDown(self):
        self.temp.cleanup()

    def _approved(self, reference='CABLE-30001', data=None, segment='S3', start=120.0, end=135.0):
        payload = dict(CREATE_DATA) if data is None else dict(data)
        payload.setdefault('segment', segment)
        payload['start_km'] = start
        payload['end_km'] = end
        record = self.service.create(Actor('creator', 'noc_operator'), reference, payload)
        return self.service.act(Actor('rm', 'repair_manager'), record['id'], record['version'], 'approve', {'repair_manager': 'RM-2'})

    def test_insufficient_batch_keeps_draft_with_shortfall(self):
        record = self._approved()
        draft = self.service.register_occupation(DISPATCHER, record['id'], occupation_payload(reserve_km=25.0))
        self.assertEqual(draft['status'], 'draft')
        self.assertEqual(draft['shortfall_km'], 5.0)
        self.assertTrue(any('缺口5.0' in note for note in draft['warnings']))
        # 草稿不扣减余量
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 20.0)
        with self.assertRaises(ResourceConflict) as caught:
            self.service.confirm_occupation(DISPATCHER, draft['id'])
        self.assertEqual(caught.exception.context['remaining_km'], 20.0)
        self.assertEqual(caught.exception.context['shortfall_km'], 5.0)

    def test_confirm_deducts_remaining(self):
        record = self._approved()
        draft = self.service.register_occupation(DISPATCHER, record['id'], occupation_payload())
        confirmed = self.service.confirm_occupation(DISPATCHER, draft['id'])
        self.assertEqual(confirmed['status'], 'active')
        self.assertEqual(confirmed['batch_remaining_km'], 2.0)
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 2.0)

    def test_concurrent_confirm_only_one_succeeds(self):
        # 批次余量20，两票各需18：只能一票成功
        first = self._approved('CABLE-30001', segment='S3')
        second = self._approved('CABLE-30002', segment='S9', start=200.0, end=210.0)
        d1 = self.service.register_occupation(DISPATCHER, first['id'], occupation_payload())
        d2 = self.service.register_occupation(Actor('disp2', 'dispatcher'), second['id'], occupation_payload(vessel_name='CS-2', splice_crew='CREW-B'))

        barrier = threading.Barrier(2)
        results = {}

        def confirm(key, draft_id, user):
            barrier.wait()
            try:
                results[key] = ('ok', self.service.confirm_occupation(Actor(user, 'dispatcher'), draft_id))
            except ResourceConflict as exc:
                results[key] = ('conflict', exc.context)

        t1 = threading.Thread(target=confirm, args=('a', d1['id'], 'disp1'))
        t2 = threading.Thread(target=confirm, args=('b', d2['id'], 'disp2'))
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)
        statuses = sorted(key for key, value in results.items() if value[0] == 'ok')
        failures = [value for value in results.values() if value[0] == 'conflict']
        self.assertEqual(len(statuses), 1)
        self.assertEqual(len(failures), 1)
        # 失败者看到的是扣减后的最新余量
        self.assertEqual(failures[0][1]['remaining_km'], 2.0)
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 2.0)

    def test_cancel_after_confirm_restores_full_reserve(self):
        record = self._approved()
        draft = self.service.register_occupation(DISPATCHER, record['id'], occupation_payload())
        self.service.confirm_occupation(DISPATCHER, draft['id'])
        self.service.cancel_occupation(DISPATCHER, record['id'])
        # approved阶段取消占用后任务本身仍可继续（此处直接取消任务），余量恢复
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 20.0)
        occupation = self.service.get_occupation(DISPATCHER, record['id'])
        self.assertEqual(occupation['status'], 'released')

    def test_cancel_mobilized_task_releases_resources(self):
        record = self._approved()
        draft = self.service.register_occupation(DISPATCHER, record['id'], occupation_payload())
        self.service.confirm_occupation(DISPATCHER, draft['id'])
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        record = self.service.act(Actor('rm', 'repair_manager'), record['id'], record['version'], 'cancel', {'cancel_reason': '天气恶化'})
        self.assertEqual(record['state'], 'cancelled')
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 20.0)

    def test_return_to_port_releases_resources(self):
        record = self._approved()
        draft = self.service.register_occupation(DISPATCHER, record['id'], occupation_payload())
        self.service.confirm_occupation(DISPATCHER, draft['id'])
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'survey',
                                  {'survey_complete': True, 'fault_location_km': 128})
        record = self.service.act(Actor('rm', 'repair_manager'), record['id'], record['version'], 'return', {'return_reason': '备件不足回港'})
        self.assertEqual(record['state'], 'returned')
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 20.0)

    def test_splice_failure_refunds_unconsumed(self):
        record = self._mobilized_and_surveyed()
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'splice_failed',
                                  {'failure_reason': '接头进水', 'consumed_km': 5.0})
        self.assertEqual(record['state'], 'splice_failed')
        # 占用18，消耗5，退回13
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 15.0)
        occupation = self.service.get_occupation(DISPATCHER, record['id'])
        self.assertEqual(occupation['status'], 'released')
        self.assertEqual(occupation['consumed_km'], 5.0)

    def test_splice_failure_over_reserve_rejected_before_state_change(self):
        record = self._mobilized_and_surveyed()
        before = self.service.get_record(Actor('ce', 'cable_engineer'), record['id'])
        with self.assertRaises(Conflict):
            self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'splice_failed',
                             {'failure_reason': '异常', 'consumed_km': 99})
        after = self.service.get_record(Actor('ce', 'cable_engineer'), record['id'])
        self.assertEqual(after['state'], before['state'])
        self.assertEqual(after['version'], before['version'])

    def test_restore_refunds_consumed_difference(self):
        record = self._mobilized_and_surveyed()
        record = self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'splice',
                                  {'splice_loss_db': 0.12, 'spare_used_km': 16})
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'test',
                                  {'end_to_end_loss_db': 0.3})
        record = self.service.act(Actor('noc', 'noc_operator'), record['id'], record['version'], 'restore',
                                  {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(record['state'], 'restored')
        # 20 - 18 + (18 - 16)
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 4.0)

    def _mobilized_and_surveyed(self):
        record = self._approved()
        draft = self.service.register_occupation(DISPATCHER, record['id'], occupation_payload())
        self.service.confirm_occupation(DISPATCHER, draft['id'])
        record = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        return self.service.act(Actor('ce', 'cable_engineer'), record['id'], record['version'], 'survey',
                                {'survey_complete': True, 'fault_location_km': 128})


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / 'test.db')
        self.service = build_service(self.db_path)
        self.service.register_spare_batch(DISPATCHER, 'BATCH-9', 100.0)

    def tearDown(self):
        self.temp.cleanup()

    def _craft_legacy(self, reference, state, payload_overrides=None):
        """绕过服务流程直接落一条旧数据。"""
        now = datetime.now(timezone.utc).isoformat()
        import json
        payload = {'required_spare_km': 18.0, 'repair_distance_km': 15.0}
        if payload_overrides:
            payload.update(payload_overrides)
        repo = Repository(self.db_path)
        with repo._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (reference, state, 1, json.dumps(payload, ensure_ascii=False), 'legacy', 'legacy', now, now),
            )
            return int(cursor.lastrowid)

    def test_backfill_mobilized_and_pending_review(self):
        known = self._craft_legacy('OLD-1', 'mobilized', {'vessel_name': 'CS-9', 'spare_batch_no': 'BATCH-9'})
        missing = self._craft_legacy('OLD-2', 'mobilized', {'vessel_name': 'CS-9'})
        restored = self._craft_legacy('OLD-3', 'restored', {'vessel_name': 'CS-9', 'spare_batch_no': 'BATCH-9', 'spare_used_km': 10.0})

        result = self.service.backfill_legacy_occupations(DISPATCHER)
        self.assertEqual(result['scanned'], 3)
        self.assertEqual({item['reference'] for item in result['backfilled']}, {'OLD-1', 'OLD-3'})
        self.assertEqual([item['reference'] for item in result['pending_review']], ['OLD-2'])

        occupation = self.service.get_occupation(DISPATCHER, known)
        self.assertEqual(occupation['status'], 'active')
        self.assertEqual(occupation['batch_no'], 'BATCH-9')
        occ_restored = self.service.get_occupation(DISPATCHER, restored)
        self.assertEqual(occ_restored['status'], 'released')
        # 已结束任务只扣实际消耗：100 - 18(active) - 10(restored)
        self.assertEqual(self.service.list_spare_batches(DISPATCHER)[0]['remaining_km'], 72.0)

        reviews = self.service.list_reviews(DISPATCHER)
        self.assertEqual([item['record_id'] for item in reviews], [missing])

        # 待核对任务不能参与新安排
        with self.assertRaises(Conflict):
            self.service.act(Actor('ce', 'cable_engineer'), missing, 1, 'survey', {'survey_complete': True, 'fault_location_km': 1})

        # 重复迁移是幂等的：已补登和待核对的都不会再次扫描
        again = self.service.backfill_legacy_occupations(DISPATCHER)
        self.assertEqual(again['scanned'], 0)

        # 补齐核对信息后解冻
        start, end = window()
        resolved = self.service.resolve_review(DISPATCHER, missing, occupation_payload(vessel_name='CS-9', batch_no='BATCH-9', splice_crew='CREW-X', window_start=start, window_end=end))
        self.assertTrue(resolved['review_resolved'])
        self.assertEqual(self.service.list_reviews(DISPATCHER), [])
        self.assertEqual(self.service.get_occupation(DISPATCHER, missing)['status'], 'active')
