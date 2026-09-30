"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import (
    OCC_CONFIRMED,
    OCC_CONSUMED,
    OCC_DISCARDED,
    OCC_DRAFT,
    OCC_RELEASED,
    RECONCILE_OK,
    RECONCILE_PENDING,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# 需要补回占用的历史状态：已经动员之后的全部任务。
LEGACY_STATES = ("mobilized", "surveyed", "spliced", "tested", "restored")
ACTIVE_STATES = ("mobilized", "surveyed")
TERMINAL_DONE_STATES = ("spliced", "tested", "restored")
LEGACY_CREW_PLACEHOLDER = "历史数据-班组待补"


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
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
                    batch_no TEXT PRIMARY KEY,
                    total_km REAL NOT NULL,
                    available_km REAL NOT NULL CHECK (available_km >= -0.000001),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_occupations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    vessel_code TEXT NOT NULL,
                    crew_code TEXT NOT NULL,
                    batch_no TEXT NOT NULL DEFAULT '',
                    reserve_km REAL NOT NULL CHECK (reserve_km >= 0),
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    shortage_km REAL NOT NULL DEFAULT 0,
                    spare_used_km REAL,
                    note TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_occ_record ON resource_occupations(record_id, status);
                CREATE INDEX IF NOT EXISTS idx_occ_status ON resource_occupations(status);
                CREATE INDEX IF NOT EXISTS idx_occ_window ON resource_occupations(window_start, window_end);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occ_open_record
                    ON resource_occupations(record_id)
                    WHERE status IN ('draft', 'confirmed');
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "reconcile_status" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN reconcile_status TEXT NOT NULL DEFAULT 'ok'")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item.setdefault("reconcile_status", RECONCILE_OK)
        return item

    @staticmethod
    def _occ_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("reserve_km", "shortage_km", "spare_used_km"):
            if item.get(key) is not None:
                item[key] = round(float(item[key]), 3)
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

    # ------------------------------------------------------------------
    # 备缆批次
    # ------------------------------------------------------------------

    def create_batch(self, batch_no: str, total_km: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO spare_batches(batch_no,total_km,available_km,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (batch_no, total_km, total_km, actor_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("备缆批次已存在") from exc
        return self.get_batch(batch_no)

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM spare_batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("备缆批次不存在")
        item = dict(row)
        item["total_km"] = round(float(item["total_km"]), 3)
        item["available_km"] = round(float(item["available_km"]), 3)
        return item

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM spare_batches ORDER BY batch_no").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["total_km"] = round(float(item["total_km"]), 3)
            item["available_km"] = round(float(item["available_km"]), 3)
            result.append(item)
        return result

    def restock_batch(self, batch_no: str, add_km: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM spare_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("备缆批次不存在")
            connection.execute(
                "UPDATE spare_batches SET total_km=total_km+?, available_km=available_km+?, updated_at=? WHERE batch_no=?",
                (add_km, add_km, now, batch_no),
            )
            result = connection.execute("SELECT * FROM spare_batches WHERE batch_no=?", (batch_no,)).fetchone()
            connection.commit()
        item = dict(result)
        item["total_km"] = round(float(item["total_km"]), 3)
        item["available_km"] = round(float(item["available_km"]), 3)
        return item

    # ------------------------------------------------------------------
    # 资源占用台账
    # ------------------------------------------------------------------

    def _window_conflicts(self, connection: sqlite3.Connection, window_start: str, window_end: str, vessel_code: str, crew_code: str, exclude_record: int) -> List[Dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT o.*, r.reference FROM resource_occupations o
            JOIN records r ON r.id = o.record_id
            WHERE o.status = ?
              AND o.record_id != ?
              AND o.window_start < ? AND o.window_end > ?
              AND (o.vessel_code = ? OR o.crew_code = ?)
            """,
            (OCC_CONFIRMED, exclude_record, window_end, window_start, vessel_code, crew_code),
        ).fetchall()
        return [self._occ_row(row) for row in rows]

    def _open_occupation(self, connection: sqlite3.Connection, record_id: int) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM resource_occupations WHERE record_id=? AND status IN (?,?)",
            (record_id, OCC_DRAFT, OCC_CONFIRMED),
        ).fetchone()

    def get_open_occupation(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = self._open_occupation(connection, record_id)
            return self._occ_row(row) if row is not None else None

    def get_occupation(self, occupation_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT o.*, r.reference FROM resource_occupations o JOIN records r ON r.id=o.record_id WHERE o.id=?",
                (occupation_id,),
            ).fetchone()
        if row is None:
            raise NotFound("占用登记不存在")
        return self._occ_row(row)

    def list_occupations(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT o.*, r.reference FROM resource_occupations o JOIN records r ON r.id=o.record_id WHERE o.status=? ORDER BY o.id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT o.*, r.reference FROM resource_occupations o JOIN records r ON r.id=o.record_id ORDER BY o.id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._occ_row(row) for row in rows]

    def save_draft(self, record_id: int, reg: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """登记（或覆盖）草稿；不扣减余量。余量缺口与时间窗冲突只提示不拦截。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT id FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            batch = connection.execute("SELECT available_km FROM spare_batches WHERE batch_no=?", (reg["batch_no"],)).fetchone()
            if batch is None:
                connection.rollback()
                raise NotFound("备缆批次不存在")
            batch_available = round(float(batch["available_km"]), 3)
            shortage = round(max(0.0, reg["reserve_km"] - float(batch["available_km"])), 3)
            warnings = self._window_conflicts(
                connection, reg["window_start"], reg["window_end"], reg["vessel_code"], reg["crew_code"], record_id
            )
            existing = self._open_occupation(connection, record_id)
            if existing is not None and existing["status"] == OCC_CONFIRMED:
                connection.rollback()
                raise Conflict("占用已确认，不能改登记；如需调整请先取消或释放")
            if existing is not None:
                connection.execute(
                    """
                    UPDATE resource_occupations SET vessel_code=?,crew_code=?,batch_no=?,reserve_km=?,
                        window_start=?,window_end=?,shortage_km=?,note=?,version=version+1,
                        updated_by=?,updated_at=? WHERE id=?
                    """,
                    (reg["vessel_code"], reg["crew_code"], reg["batch_no"], reg["reserve_km"],
                     reg["window_start"], reg["window_end"], shortage, reg["note"], actor_id, now, existing["id"]),
                )
                occ_id = int(existing["id"])
                action = "occupy_draft_updated"
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO resource_occupations(record_id,status,vessel_code,crew_code,batch_no,reserve_km,
                        window_start,window_end,shortage_km,note,version,created_by,updated_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)
                    """,
                    (record_id, OCC_DRAFT, reg["vessel_code"], reg["crew_code"], reg["batch_no"], reg["reserve_km"],
                     reg["window_start"], reg["window_end"], shortage, reg["note"], actor_id, actor_id, now, now),
                )
                occ_id = int(cursor.lastrowid)
                action = "occupy_drafted"
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, 0,
                 json.dumps({"shortage_km": shortage, "batch_available_km": batch_available,
                             "window_conflicts": warnings}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute(
                "SELECT o.*, r.reference FROM resource_occupations o JOIN records r ON r.id=o.record_id WHERE o.id=?",
                (occ_id,),
            ).fetchone()
            connection.commit()
        result = self._occ_row(row)
        result["batch_available_km"] = batch_available
        result["window_conflicts"] = warnings
        return result

    def confirm_occupation(self, record_id: int, expected_version: int, actor_id: str) -> Dict[str, Any]:
        """并发安全的占用确认：全事务内校验时间窗互斥与备缆余量，扣减余量。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if record["state"] != "approved":
                connection.rollback()
                raise Conflict("占用确认必须在动员前（方案已批准状态）完成")
            occ = self._open_occupation(connection, record_id)
            if occ is None:
                connection.rollback()
                raise Conflict("尚未登记资源占用，请先保存草稿")
            if occ["status"] == OCC_CONFIRMED:
                connection.rollback()
                raise Conflict("占用已确认，请勿重复提交")
            if int(occ["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("占用登记已被修改，请刷新最新余量后重试", {
                    "latest_version": int(occ["version"]),
                    "shortage_km": round(float(occ["shortage_km"]), 3),
                })
            conflicts = self._window_conflicts(
                connection, occ["window_start"], occ["window_end"], occ["vessel_code"], occ["crew_code"], record_id
            )
            batch = connection.execute("SELECT * FROM spare_batches WHERE batch_no=?", (occ["batch_no"],)).fetchone()
            batch_available = round(float(batch["available_km"]), 3) if batch is not None else 0.0
            if conflicts:
                connection.rollback()
                raise Conflict("船机或接续班组在同一时间窗已被其他抢修占用", {
                    "conflicts": [self._occ_row(item) for item in conflicts],
                    "batch_available_km": batch_available,
                })
            if batch is None:
                connection.rollback()
                raise NotFound("备缆批次不存在")
            shortage = round(float(occ["reserve_km"]) - float(batch["available_km"]), 3)
            if shortage > 0:
                connection.rollback()
                raise Conflict("备缆余量不足，草稿已保留", {
                    "batch_no": occ["batch_no"],
                    "reserve_km": round(float(occ["reserve_km"]), 3),
                    "batch_available_km": batch_available,
                    "shortage_km": shortage,
                })
            connection.execute(
                "UPDATE spare_batches SET available_km=available_km-?, updated_at=? WHERE batch_no=?",
                (occ["reserve_km"], now, occ["batch_no"]),
            )
            connection.execute(
                "UPDATE resource_occupations SET status=?, shortage_km=0, version=version+1, confirmed_at=?, updated_by=?, updated_at=? WHERE id=?",
                (OCC_CONFIRMED, now, actor_id, now, occ["id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "occupy_confirmed", actor_id, 0,
                 json.dumps({"vessel_code": occ["vessel_code"], "crew_code": occ["crew_code"],
                             "batch_no": occ["batch_no"], "reserve_km": round(float(occ["reserve_km"]), 3),
                             "batch_available_km": round(batch_available - float(occ["reserve_km"]), 3)},
                            ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute(
                "SELECT o.*, r.reference FROM resource_occupations o JOIN records r ON r.id=o.record_id WHERE o.id=?",
                (occ["id"],),
            ).fetchone()
            fresh = connection.execute("SELECT available_km FROM spare_batches WHERE batch_no=?", (occ["batch_no"],)).fetchone()
            connection.commit()
        result = self._occ_row(row)
        result["batch_available_km"] = round(float(fresh["available_km"]), 3)
        return result

    def mutate_with_ledger(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        ledger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """状态变更与占用台账在同一事务内结算：动员锁定、接续结算、取消/回港/失败释放。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
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
            if ledger is not None:
                occ = self._open_occupation(connection, record_id)
                op = ledger["op"]
                if op == "activate":
                    if occ is None or occ["status"] != OCC_CONFIRMED:
                        connection.rollback()
                        raise Conflict("资源占用尚未确认，不能动员")
                elif op == "consume":
                    if occ is None:
                        connection.rollback()
                        raise Conflict("任务缺少资源占用登记，不能执行该动作")
                    used = float(ledger["used_km"])
                    if occ["status"] != OCC_CONFIRMED:
                        connection.rollback()
                        raise Conflict("资源占用尚未确认，不能接续")
                    if used > float(occ["reserve_km"]) + 1e-9:
                        connection.rollback()
                        raise Conflict("实际消耗超过占用公里数，请追加占用登记", {
                            "reserve_km": round(float(occ["reserve_km"]), 3),
                            "used_km": round(used, 3),
                        })
                    restored = round(float(occ["reserve_km"]) - used, 3)
                    connection.execute(
                        "UPDATE spare_batches SET available_km=available_km+?, updated_at=? WHERE batch_no=?",
                        (restored, now, occ["batch_no"]),
                    )
                    connection.execute(
                        "UPDATE resource_occupations SET status=?, spare_used_km=?, version=version+1, updated_by=?, updated_at=? WHERE id=?",
                        (OCC_CONSUMED, used, actor_id, now, occ["id"]),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (record_id, "occupy_consumed", actor_id, 0,
                         json.dumps({"batch_no": occ["batch_no"], "reserve_km": round(float(occ["reserve_km"]), 3),
                                     "used_km": round(used, 3), "restored_km": restored}, ensure_ascii=False, sort_keys=True), now),
                    )
                elif op == "release":
                    if occ is None:
                        # 动员前（含尚未登记占用）取消：无占用可释放，直接落状态。
                        pass
                    elif occ["status"] == OCC_CONFIRMED:
                        restored = round(float(occ["reserve_km"]), 3)
                        connection.execute(
                            "UPDATE spare_batches SET available_km=available_km+?, updated_at=? WHERE batch_no=?",
                            (restored, now, occ["batch_no"]),
                        )
                        new_status = OCC_RELEASED
                        connection.execute(
                            "UPDATE resource_occupations SET status=?, shortage_km=0, version=version+1, updated_by=?, updated_at=? WHERE id=?",
                            (new_status, actor_id, now, occ["id"]),
                        )
                        connection.execute(
                            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                            (record_id, "occupy_released", actor_id, 0,
                             json.dumps({"batch_no": occ["batch_no"], "restored_km": restored,
                                         "reason": ledger.get("reason", "")}, ensure_ascii=False, sort_keys=True), now),
                        )
                    else:
                        connection.execute(
                            "UPDATE resource_occupations SET status=?, shortage_km=0, version=version+1, updated_by=?, updated_at=? WHERE id=?",
                            (OCC_DISCARDED, actor_id, now, occ["id"]),
                        )
                        connection.execute(
                            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                            (record_id, "occupy_discarded", actor_id, 0,
                             json.dumps({"reason": ledger.get("reason", "")}, ensure_ascii=False, sort_keys=True), now),
                        )
                else:
                    connection.rollback()
                    raise Conflict("未知的台账结算类型")
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ------------------------------------------------------------------
    # 旧数据迁移与待核对
    # ------------------------------------------------------------------

    def list_pending_reconciliation(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE reconcile_status=? ORDER BY id", (RECONCILE_PENDING,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def backfill_legacy_occupations(self, actor_id: str) -> Dict[str, Any]:
        """为已动员但没有台账的历史任务补回占用；批次无法落实的进待核对。"""
        now = _now()
        backfilled: List[int] = []
        pending: List[int] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM records WHERE state IN (%s) ORDER BY id" % ",".join("?" for _ in LEGACY_STATES),
                LEGACY_STATES,
            ).fetchall()
            for row in rows:
                record = self._row(row)
                exists = connection.execute(
                    "SELECT COUNT(*) AS total FROM resource_occupations WHERE record_id=?", (record["id"],)
                ).fetchone()
                if int(exists["total"]) > 0:
                    continue
                payload = record["payload"]
                vessel = str(payload.get("vessel_name") or "").strip()
                crew = str(payload.get("crew_code") or LEGACY_CREW_PLACEHOLDER).strip()
                reserve = round(float(payload.get("spare_used_km") or payload.get("required_spare_km") or 0), 3)
                batch_no = str(payload.get("batch_no") or "").strip()
                batch = connection.execute("SELECT * FROM spare_batches WHERE batch_no=?", (batch_no,)).fetchone() if batch_no else None
                consumed = record["state"] in TERMINAL_DONE_STATES
                insufficient = batch is not None and not consumed and float(batch["available_km"]) < reserve
                if batch is None or insufficient:
                    status = OCC_DRAFT
                    shortage = round(reserve - float(batch["available_km"]), 3) if insufficient else reserve
                    connection.execute(
                        """
                        INSERT INTO resource_occupations(record_id,status,vessel_code,crew_code,batch_no,reserve_km,
                            window_start,window_end,shortage_km,note,version,created_by,updated_by,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)
                        """,
                        (record["id"], status, vessel or LEGACY_CREW_PLACEHOLDER, crew, batch_no, reserve,
                         now, now, shortage, "旧数据迁移：批次缺失或余量不足，待核对",
                         actor_id, actor_id, now, now),
                    )
                    connection.execute(
                        "UPDATE records SET reconcile_status=? WHERE id=?", (RECONCILE_PENDING, record["id"])
                    )
                    pending.append(record["id"])
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (record["id"], "legacy_backfill_pending", actor_id, int(row["version"]),
                         json.dumps({"reason": "批次无法落实" if batch is None else "备缆余量不足",
                                     "batch_no": batch_no, "shortage_km": shortage}, ensure_ascii=False, sort_keys=True), now),
                    )
                    continue
                if consumed:
                    window_start = record["created_at"]
                    window_end = (datetime.fromisoformat(record["created_at"]) + timedelta(seconds=1)).isoformat()
                    used = round(float(payload.get("spare_used_km") or reserve), 3)
                    connection.execute(
                        """
                        INSERT INTO resource_occupations(record_id,status,vessel_code,crew_code,batch_no,reserve_km,
                            window_start,window_end,shortage_km,spare_used_km,note,version,created_by,updated_by,created_at,confirmed_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)
                        """,
                        (record["id"], OCC_CONSUMED, vessel, crew, batch_no, reserve,
                         window_start, window_end, 0, used, "旧数据迁移补回",
                         actor_id, actor_id, now, record["created_at"], now),
                    )
                else:
                    window_end_dt = datetime.now(timezone.utc) + timedelta(hours=float(payload.get("estimated_repair_hours") or 24))
                    window_end = window_end_dt.replace(microsecond=0).isoformat()
                    connection.execute(
                        """
                        INSERT INTO resource_occupations(record_id,status,vessel_code,crew_code,batch_no,reserve_km,
                            window_start,window_end,shortage_km,note,version,created_by,updated_by,created_at,confirmed_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)
                        """,
                        (record["id"], OCC_CONFIRMED, vessel, crew, batch_no, reserve,
                         now, window_end, 0, "旧数据迁移补回", actor_id, actor_id, now, now, now),
                    )
                    connection.execute(
                        "UPDATE spare_batches SET available_km=available_km-?, updated_at=? WHERE batch_no=?",
                        (reserve, now, batch_no),
                    )
                backfilled.append(record["id"])
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record["id"], "legacy_backfilled", actor_id, int(row["version"]),
                     json.dumps({"batch_no": batch_no, "reserve_km": reserve, "consumed": consumed}, ensure_ascii=False, sort_keys=True), now),
                )
            connection.commit()
        return {"backfilled": backfilled, "pending": pending}

    def reconcile_occupation(self, record_id: int, batch_no: str, reserve_km: float, actor_id: str) -> Dict[str, Any]:
        """补齐待核对任务的批次与占用公里数，使其重新参与安排。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if record["reconcile_status"] != RECONCILE_PENDING:
                connection.rollback()
                raise Conflict("该任务无需核对")
            batch = connection.execute("SELECT * FROM spare_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if batch is None:
                connection.rollback()
                raise NotFound("备缆批次不存在")
            occ = self._open_occupation(connection, record_id)
            if occ is None:
                connection.rollback()
                raise Conflict("待核对任务缺少占用草稿")
            consumed = record["state"] in TERMINAL_DONE_STATES
            if not consumed and float(batch["available_km"]) < reserve_km:
                connection.rollback()
                raise Conflict("备缆余量仍不足，继续保持待核对", {
                    "batch_no": batch_no,
                    "batch_available_km": round(float(batch["available_km"]), 3),
                    "shortage_km": round(reserve_km - float(batch["available_km"]), 3),
                })
            if consumed:
                used = round(float(json.loads(record["payload"]).get("spare_used_km") or reserve_km), 3)
                connection.execute(
                    "UPDATE resource_occupations SET batch_no=?,reserve_km=?,shortage_km=0,spare_used_km=?,status=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
                    (batch_no, reserve_km, used, OCC_CONSUMED, actor_id, now, occ["id"]),
                )
            else:
                connection.execute(
                    "UPDATE spare_batches SET available_km=available_km-?, updated_at=? WHERE batch_no=?",
                    (reserve_km, now, batch_no),
                )
                connection.execute(
                    "UPDATE resource_occupations SET batch_no=?,reserve_km=?,shortage_km=0,status=?,confirmed_at=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
                    (batch_no, reserve_km, OCC_CONFIRMED, now, actor_id, now, occ["id"]),
                )
            connection.execute("UPDATE records SET reconcile_status=? WHERE id=?", (RECONCILE_OK, record_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "occupation_reconciled", actor_id, int(record["version"]),
                 json.dumps({"batch_no": batch_no, "reserve_km": reserve_km, "consumed": consumed}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute(
                "SELECT o.*, r.reference FROM resource_occupations o JOIN records r ON r.id=o.record_id WHERE o.id=?",
                (occ["id"],),
            ).fetchone()
            connection.commit()
        return self._occ_row(row)

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

    def stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
            pending = connection.execute(
                "SELECT COUNT(*) AS total FROM records WHERE reconcile_status=?", (RECONCILE_PENDING,)
            ).fetchone()
        result = {str(row["state"]): int(row["total"]) for row in rows}
        result["pending_reconciliation"] = int(pending["total"])
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
