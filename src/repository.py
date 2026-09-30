"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ResourceConflict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


OCCUPATION_DRAFT = "draft"
OCCUPATION_ACTIVE = "active"
OCCUPATION_RELEASED = "released"

MIGRATABLE_STATES = ("mobilized", "surveyed", "spliced", "tested", "restored")


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spare_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    total_km REAL NOT NULL,
                    remaining_km REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_occupations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL UNIQUE REFERENCES records(id) ON DELETE CASCADE,
                    vessel_name TEXT NOT NULL,
                    splice_crew TEXT NOT NULL,
                    batch_id INTEGER NOT NULL REFERENCES spare_batches(id),
                    reserve_km REAL NOT NULL,
                    consumed_km REAL NOT NULL DEFAULT 0,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    shortfall_km REAL NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    released_at TEXT
                );
                CREATE TABLE IF NOT EXISTS record_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL UNIQUE REFERENCES records(id) ON DELETE CASCADE,
                    reason TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_occupations_status ON resource_occupations(status);
                CREATE INDEX IF NOT EXISTS idx_occupations_batch ON resource_occupations(batch_id);
                CREATE INDEX IF NOT EXISTS idx_reviews_open ON record_reviews(resolved);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 备缆批次 -------------------------------------------------------

    def create_spare_batch(self, batch_no: str, total_km: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO spare_batches(batch_no,total_km,remaining_km,created_by,created_at) VALUES(?,?,?,?,?)",
                    (batch_no, total_km, total_km, actor_id, now),
                )
                row = connection.execute("SELECT * FROM spare_batches WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("备缆批次已存在") from exc
        return self._batch_row(row)

    def get_spare_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM spare_batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("备缆批次不存在")
        return self._batch_row(row)

    def list_spare_batches(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM spare_batches ORDER BY id").fetchall()
        return [self._batch_row(row) for row in rows]

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {"id": int(row["id"]), "batch_no": row["batch_no"], "total_km": float(row["total_km"]), "remaining_km": float(row["remaining_km"]), "created_by": row["created_by"], "created_at": row["created_at"]}

    # ---- 待核对 ---------------------------------------------------------

    def open_review(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM record_reviews WHERE record_id=? AND resolved=0", (record_id,)).fetchone()
        return self._review_row(row) if row is not None else None

    def list_open_reviews(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rv.*, r.reference AS reference FROM record_reviews rv JOIN records r ON r.id = rv.record_id WHERE rv.resolved=0 ORDER BY rv.id"
            ).fetchall()
        return [self._review_row(row, reference=row["reference"]) for row in rows]

    def create_review(self, record_id: int, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO record_reviews(record_id,reason,resolved,created_by,created_at) VALUES(?,?,'0',?,?) "
                "ON CONFLICT(record_id) DO UPDATE SET reason=excluded.reason, resolved=0, resolved_at=NULL",
                (record_id, reason, actor_id, now),
            )
            row = connection.execute("SELECT * FROM record_reviews WHERE record_id=?", (record_id,)).fetchone()
        return self._review_row(row)

    def resolve_review(self, record_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE record_reviews SET resolved=1, resolved_at=? WHERE record_id=? AND resolved=0", (_now(), record_id))

    @staticmethod
    def _review_row(row: sqlite3.Row, reference: str = "") -> Dict[str, Any]:
        keys = set(row.keys())
        ref = row["reference"] if "reference" in keys else reference
        return {"id": int(row["id"]), "record_id": int(row["record_id"]), "reason": row["reason"], "resolved": bool(row["resolved"]), "created_by": row["created_by"], "created_at": row["created_at"], "resolved_at": row["resolved_at"], "reference": ref}

    # ---- 资源占用台账 ---------------------------------------------------

    @staticmethod
    def _occupation_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "record_id": int(row["record_id"]),
            "reference": row["reference"],
            "vessel_name": row["vessel_name"],
            "splice_crew": row["splice_crew"],
            "batch_no": row["batch_no"],
            "reserve_km": float(row["reserve_km"]),
            "consumed_km": float(row["consumed_km"]),
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "status": row["status"],
            "shortfall_km": float(row["shortfall_km"]),
            "notes": row["notes"],
            "created_by": row["created_by"],
            "confirmed_by": row["confirmed_by"],
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
            "released_at": row["released_at"],
        }

    _OCCUPATION_SELECT = (
        "SELECT o.*, b.batch_no AS batch_no, r.reference AS reference "
        "FROM resource_occupations o JOIN spare_batches b ON b.id = o.batch_id JOIN records r ON r.id = o.record_id"
    )

    def get_occupation(self, occupation_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(self._OCCUPATION_SELECT + " WHERE o.id=?", (occupation_id,)).fetchone()
        if row is None:
            raise NotFound("占用台账不存在")
        return self._occupation_row(row)

    def find_occupation(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(self._OCCUPATION_SELECT + " WHERE o.record_id=?", (record_id,)).fetchone()
        return self._occupation_row(row) if row is not None else None

    def list_occupations(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute(self._OCCUPATION_SELECT + " WHERE o.status=? ORDER BY o.id", (status,)).fetchall()
            else:
                rows = connection.execute(self._OCCUPATION_SELECT + " ORDER BY o.id").fetchall()
        return [self._occupation_row(row) for row in rows]

    def upsert_draft(self, record_id: int, fields: Dict[str, Any], batch_id: int, shortfall_km: float, notes: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id, status FROM resource_occupations WHERE record_id=?", (record_id,)).fetchone()
            if row is not None and row["status"] != OCCUPATION_DRAFT:
                connection.rollback()
                raise Conflict("该任务已有生效中的占用，不能重复登记")
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO resource_occupations(record_id,vessel_name,splice_crew,batch_id,reserve_km,"
                    "window_start,window_end,status,shortfall_km,notes,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, fields["vessel_name"], fields["splice_crew"], batch_id, fields["reserve_km"],
                     fields["window_start"], fields["window_end"], OCCUPATION_DRAFT, shortfall_km, notes, actor_id, now),
                )
                occupation_id = int(cursor.lastrowid)
            else:
                occupation_id = int(row["id"])
                connection.execute(
                    "UPDATE resource_occupations SET vessel_name=?,splice_crew=?,batch_id=?,reserve_km=?,"
                    "consumed_km=0,window_start=?,window_end=?,shortfall_km=?,notes=?,created_by=?,created_at=?,"
                    "confirmed_by=NULL,confirmed_at=NULL,released_at=NULL WHERE id=?",
                    (fields["vessel_name"], fields["splice_crew"], batch_id, fields["reserve_km"],
                     fields["window_start"], fields["window_end"], shortfall_km, notes, actor_id, now, occupation_id),
                )
            result = connection.execute(self._OCCUPATION_SELECT + " WHERE o.id=?", (occupation_id,)).fetchone()
            connection.commit()
        return self._occupation_row(result)

    def discard_draft(self, record_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM resource_occupations WHERE record_id=? AND status=?", (record_id, OCCUPATION_DRAFT))

    def confirm_occupation(self, occupation_id: int, actor_id: str) -> Dict[str, Any]:
        """原子确认占用：时间窗查重与备缆余量扣减在同一写事务内完成。

        两个调度员并发确认时，只有一个事务能扣减成功；失败者拿到最新余量。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(self._OCCUPATION_SELECT + " WHERE o.id=?", (occupation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("占用台账不存在")
            if row["status"] != OCCUPATION_DRAFT:
                connection.rollback()
                raise Conflict("占用草稿不是待确认状态")
            review = connection.execute("SELECT 1 FROM record_reviews WHERE record_id=? AND resolved=0", (row["record_id"],)).fetchone()
            if review is not None:
                connection.rollback()
                raise Conflict("任务处于待核对状态，补齐前不参与新安排")
            clash = connection.execute(
                self._OCCUPATION_SELECT
                + " WHERE o.status=? AND o.id<>? AND ("
                "  (o.vessel_name=? AND o.window_start < ? AND ? < o.window_end)"
                "  OR (o.splice_crew=? AND o.window_start < ? AND ? < o.window_end)) LIMIT 1",
                (OCCUPATION_ACTIVE, occupation_id,
                 row["vessel_name"], row["window_end"], row["window_start"],
                 row["splice_crew"], row["window_end"], row["window_start"]),
            ).fetchone()
            if clash is not None:
                connection.rollback()
                which = "船机" if clash["vessel_name"] == row["vessel_name"] else "接续班组"
                raise ResourceConflict(
                    "%s%s在同一时间窗已被任务%s占用" % (which, row["vessel_name"] if which == "船机" else row["splice_crew"], clash["reference"]),
                    {"clash_with": clash["reference"], "resource_type": "vessel" if which == "船机" else "crew"},
                )
            updated = connection.execute(
                "UPDATE spare_batches SET remaining_km = remaining_km - ? WHERE id=? AND remaining_km >= ?",
                (row["reserve_km"], row["batch_id"], row["reserve_km"]),
            )
            if updated.rowcount == 0:
                latest = connection.execute("SELECT remaining_km FROM spare_batches WHERE id=?", (row["batch_id"],)).fetchone()
                connection.rollback()
                raise ResourceConflict(
                    "备缆批次%s余量不足" % row["batch_no"],
                    {"batch_no": row["batch_no"], "remaining_km": float(latest["remaining_km"]) if latest else None, "required_km": float(row["reserve_km"]), "shortfall_km": round(float(row["reserve_km"]) - float(latest["remaining_km"] if latest else 0), 2)},
                )
            connection.execute(
                "UPDATE resource_occupations SET status=?,shortfall_km=0,notes='',confirmed_by=?,confirmed_at=? WHERE id=?",
                (OCCUPATION_ACTIVE, actor_id, now, occupation_id),
            )
            result = connection.execute(self._OCCUPATION_SELECT + " WHERE o.id=?", (occupation_id,)).fetchone()
            batch = connection.execute("SELECT remaining_km FROM spare_batches WHERE id=?", (row["batch_id"],)).fetchone()
            connection.commit()
        outcome = self._occupation_row(result)
        outcome["batch_remaining_km"] = float(batch["remaining_km"])
        return outcome

    def release_occupation(self, record_id: int, consumed_km: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """释放占用：船机班组退出占用，未消耗的备缆退回批次余量。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(self._OCCUPATION_SELECT + " WHERE o.record_id=?", (record_id,)).fetchone()
            if row is None or row["status"] != OCCUPATION_ACTIVE:
                connection.rollback()
                return None
            consumed = float(row["consumed_km"]) if consumed_km is None else float(consumed_km)
            if consumed < 0 or consumed > float(row["reserve_km"]):
                connection.rollback()
                raise Conflict("消耗公里数超出占用范围")
            refund = round(float(row["reserve_km"]) - consumed, 2)
            connection.execute("UPDATE spare_batches SET remaining_km = remaining_km + ? WHERE id=?", (refund, row["batch_id"]))
            connection.execute(
                "UPDATE resource_occupations SET status=?,consumed_km=?,released_at=? WHERE id=?",
                (OCCUPATION_RELEASED, consumed, now, row["id"]),
            )
            result = connection.execute(self._OCCUPATION_SELECT + " WHERE o.id=?", (row["id"],)).fetchone()
            batch = connection.execute("SELECT remaining_km FROM spare_batches WHERE id=?", (row["batch_id"],)).fetchone()
            connection.commit()
        outcome = self._occupation_row(result)
        outcome["refunded_km"] = refund
        outcome["batch_remaining_km"] = float(batch["remaining_km"])
        return outcome

    def mark_occupation_consumed(self, record_id: int, consumed_km: float) -> None:
        """登记实际消耗（接续完成时），余量在终态结算时退还差额。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT reserve_km, status FROM resource_occupations WHERE record_id=?", (record_id,)).fetchone()
            if row is None or row["status"] != OCCUPATION_ACTIVE:
                connection.rollback()
                raise Conflict("任务没有生效中的资源占用")
            if float(consumed_km) > float(row["reserve_km"]):
                connection.rollback()
                raise Conflict("备缆消耗不能超过占用公里数")
            connection.execute("UPDATE resource_occupations SET consumed_km=? WHERE record_id=?", (float(consumed_km), record_id))
            connection.commit()

    # ---- 旧数据迁移 -----------------------------------------------------

    def migration_candidates(self) -> List[Dict[str, Any]]:
        """已动员但没有占用台账、也没有未决待核对的任务。"""
        placeholders = ",".join("?" for _ in MIGRATABLE_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE state IN (%s) AND id NOT IN (SELECT record_id FROM resource_occupations) "
                "AND id NOT IN (SELECT record_id FROM record_reviews WHERE resolved=0) ORDER BY id" % placeholders,
                MIGRATABLE_STATES,
            ).fetchall()
        return [self._row(row) for row in rows]

    def backfill_occupation(self, record_id: int, vessel_name: str, splice_crew: str, batch_id: int, reserve_km: float,
                            window_start: str, window_end: str, consumed_km: float, actor_id: str) -> Dict[str, Any]:
        """迁移补登：直接按补登结果落台账；已结束任务立刻结算释放。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state_row = connection.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
            if state_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            exists = connection.execute("SELECT 1 FROM resource_occupations WHERE record_id=?", (record_id,)).fetchone()
            if exists is not None:
                connection.rollback()
                raise Conflict("任务已补登占用")
            settled = state_row["state"] == "restored"
            status = OCCUPATION_RELEASED if settled else OCCUPATION_ACTIVE
            # 进行中任务按占用量冻结余量；已结束任务只实际扣减消耗量
            held = consumed_km if settled else reserve_km
            updated = connection.execute(
                "UPDATE spare_batches SET remaining_km = remaining_km - ? WHERE id=? AND remaining_km >= ?",
                (held, batch_id, held),
            )
            if updated.rowcount == 0:
                latest = connection.execute("SELECT remaining_km FROM spare_batches WHERE id=?", (batch_id,)).fetchone()
                connection.rollback()
                raise ResourceConflict(
                    "备缆批次余量不足以补登",
                    {"remaining_km": float(latest["remaining_km"]) if latest else None, "required_km": held},
                )
            cursor = connection.execute(
                "INSERT INTO resource_occupations(record_id,vessel_name,splice_crew,batch_id,reserve_km,consumed_km,"
                "window_start,window_end,status,notes,created_by,confirmed_by,created_at,confirmed_at,released_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, vessel_name, splice_crew, batch_id, reserve_km, consumed_km,
                 window_start, window_end, status, "旧数据迁移补登", actor_id, actor_id, now, now, now if settled else None),
            )
            result = connection.execute(self._OCCUPATION_SELECT + " WHERE o.id=?", (int(cursor.lastrowid),)).fetchone()
            batch = connection.execute("SELECT remaining_km FROM spare_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        outcome = self._occupation_row(result)
        outcome["batch_remaining_km"] = float(batch["remaining_km"])
        return outcome
