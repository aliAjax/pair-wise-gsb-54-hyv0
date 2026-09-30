"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, number, text
from .repository import Repository
from .rules import CONSUME_ACTIONS, RELEASE_ACTIONS, RECONCILE_PENDING, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_ledger_role(self, actor: Actor) -> None:
        if not self.rules.role_can_manage_ledger(actor.role):
            raise PermissionDenied("角色无权管理资源占用台账")

    def _require_admin(self, actor: Actor) -> None:
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可执行旧数据迁移")

    @staticmethod
    def _ensure_not_pending(record: Dict[str, Any]) -> None:
        if record.get("reconcile_status") == RECONCILE_PENDING:
            raise Conflict("任务待核对：批次信息补齐前不参与新安排")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self._ensure_not_pending(record)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        ledger = None
        if action == "mobilize":
            occ = self.repository.get_open_occupation(record_id)
            if occ is None or occ["status"] != "confirmed":
                raise Conflict("动员前必须完成资源占用登记与确认")
            if occ["vessel_code"] != str((data or {}).get("vessel_name", "")).strip():
                raise Conflict("动员船机必须与占用登记一致", {"occupied_vessel_code": occ["vessel_code"]})
            ledger = {"op": "activate"}
        elif action in CONSUME_ACTIONS:
            ledger = {"op": "consume", "used_km": float((data or {}).get("spare_used_km", 0))}
        elif action in RELEASE_ACTIONS:
            ledger = {"op": "release", "reason": str((data or {}).get("cancel_reason") or (data or {}).get("reason") or "")}
        return self.repository.mutate_with_ledger(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            ledger=ledger,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------------
    # 备缆批次
    # ------------------------------------------------------------------

    def create_batch(self, actor: Actor, batch_no: str, total_km: float, data: Dict[str, Any] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_ledger_role(actor)
        batch_no = text({"batch_no": batch_no}, "batch_no")
        total_km = number({"total_km": total_km}, "total_km", 0.01)
        return self.repository.create_batch(batch_no, total_km, actor.user_id)

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_batch(batch_no)

    def list_batches(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches()

    def restock_batch(self, actor: Actor, batch_no: str, add_km: float) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_ledger_role(actor)
        batch_no = text({"batch_no": batch_no}, "batch_no")
        add_km = number({"add_km": add_km}, "add_km", 0.01)
        return self.repository.restock_batch(batch_no, add_km, actor.user_id)

    # ------------------------------------------------------------------
    # 资源占用台账
    # ------------------------------------------------------------------

    def register_occupation(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """动员前登记占用（草稿）；备缆不足时保留草稿并写清缺口。"""
        actor = self._actor(actor)
        self._require_ledger_role(actor)
        record = self.repository.get(record_id)
        self._ensure_not_pending(record)
        if record["state"] != "approved":
            raise Conflict("仅已批准、未动员的任务可以登记资源占用")
        reg = self.rules.validate_occupation(data or {})
        if reg["reserve_km"] < float(record["payload"].get("required_spare_km", 0)):
            raise Conflict("占用公里数不能少于任务需求 %s" % record["payload"]["required_spare_km"],
                           {"required_spare_km": record["payload"]["required_spare_km"]})
        return self.repository.save_draft(record_id, reg, actor.user_id)

    def confirm_occupation(self, actor: Actor, record_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_ledger_role(actor)
        record = self.repository.get(record_id)
        self._ensure_not_pending(record)
        if not isinstance(expected_version, int):
            raise Conflict("expected_version必须是整数")
        return self.repository.confirm_occupation(record_id, expected_version, actor.user_id)

    def list_occupations(self, actor: Actor, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_occupations(status=status, limit=limit)

    def get_occupation(self, actor: Actor, occupation_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_occupation(occupation_id)

    def list_pending(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_pending_reconciliation()

    def reconcile_occupation(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_ledger_role(actor)
        data = data or {}
        batch_no = text(data, "batch_no")
        reserve_km = number(data, "reserve_km", 0.01)
        return self.repository.reconcile_occupation(record_id, batch_no, reserve_km, actor.user_id)

    def migrate_legacy(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_admin(actor)
        return self.repository.backfill_legacy_occupations(actor.user_id)
