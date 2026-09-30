"""业务用例编排、权限检查与审计。"""
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError, number, parse_iso, text, windows_overlap
from .repository import OCCUPATION_ACTIVE, OCCUPATION_DRAFT, OCCUPATION_RELEASED, Repository
from .rules import (
    DISPATCH_ROLES,
    RESOURCE_TERMINAL_STATES,
    DomainRules,
)


REVIEW_MISSING_BATCH = "找不到备缆批次，旧数据迁移待核对"


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

    def _is_admin_or(self, actor: Actor, roles: set) -> bool:
        return actor.role == "admin" or actor.role in roles

    def _assert_not_in_review(self, record_id: int) -> None:
        review = self.repository.open_review(record_id)
        if review is not None:
            raise Conflict("任务待核对（%s），补齐前不参与新安排" % review["reason"])

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
        self._assert_not_in_review(record_id)
        self.rules.require_transition(record, action)
        occupation = self.repository.find_occupation(record_id)
        active_occupation = occupation if occupation and occupation["status"] == OCCUPATION_ACTIVE else None
        self._check_action_resources(action, record, data or {}, active_occupation)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        self._settle_resources(action, record_id, new_state, data or {}, actor.user_id, summary)
        return result

    def _check_action_resources(self, action: str, record: Dict[str, Any], data: Dict[str, Any],
                                occupation: Optional[Dict[str, Any]]) -> None:
        if action == "mobilize":
            if occupation is None:
                raise Conflict("动员前必须先登记并确认资源占用")
            if occupation["vessel_name"] != text(data, "vessel_name"):
                raise Conflict("动员船机与已确认占用的船机不一致")
            required = float(record["payload"]["required_spare_km"])
            if occupation["reserve_km"] < required:
                raise Conflict("已确认占用备缆%.2f公里，少于需求%.2f公里" % (occupation["reserve_km"], required))
        elif action == "splice":
            if occupation is None:
                raise Conflict("任务缺少生效中的资源占用")
            if float(data.get("spare_used_km", 0)) > occupation["reserve_km"]:
                raise Conflict("备缆消耗不能超过占用公里数%.2f" % occupation["reserve_km"])
        elif action == "splice_failed":
            if occupation is None:
                raise Conflict("任务缺少生效中的资源占用")
            if float(data.get("consumed_km", 0)) > occupation["reserve_km"]:
                raise Conflict("消耗备缆不能超过占用公里数%.2f" % occupation["reserve_km"])
        elif action in ("survey", "test", "restore"):
            if occupation is None:
                raise Conflict("任务缺少生效中的资源占用")
        elif action == "return" and record["state"] != "splice_failed":
            # 接续失败时占用已释放，之后的回港不再要求生效占用
            if occupation is None:
                raise Conflict("任务缺少生效中的资源占用")

    def _settle_resources(self, action: str, record_id: int, new_state: str, data: Dict[str, Any],
                          actor_id: str, summary: str) -> None:
        release_info: Optional[Dict[str, Any]] = None
        if action == "splice":
            self.repository.mark_occupation_consumed(record_id, float(data["spare_used_km"]))
        elif new_state in RESOURCE_TERMINAL_STATES:
            if action == "splice_failed":
                release_info = self.repository.release_occupation(record_id, float(data.get("consumed_km", 0)))
            else:
                # 取消/回港：未消耗占用全部恢复；恢复：退还占用与实际消耗的差额
                release_info = self.repository.release_occupation(record_id)
        elif action == "cancel":
            # approved 阶段取消：可能只留了草稿，直接丢弃
            self.repository.discard_draft(record_id)
        if release_info is not None:
            self.audit.note(
                record_id,
                actor_id,
                "resource_released",
                {"summary": summary, "refunded_km": release_info["refunded_km"], "consumed_km": release_info["consumed_km"], "batch_no": release_info["batch_no"], "remaining_km": release_info["batch_remaining_km"]},
            )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 备缆批次 -------------------------------------------------------

    def register_spare_batch(self, actor: Actor, batch_no: str, total_km: float) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_admin_or(actor, DISPATCH_ROLES | {'repair_manager'}):
            raise PermissionDenied("角色无权登记备缆批次")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        total_km = number({"total_km": total_km}, "total_km", 0.01)
        return self.repository.create_spare_batch(batch_no, total_km, actor.user_id)

    def list_spare_batches(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_spare_batches()

    # ---- 资源占用台账 ---------------------------------------------------

    def register_occupation(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """任务动员前登记资源占用。余量不足时保留草稿并写清缺口。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_admin_or(actor, DISPATCH_ROLES):
            raise PermissionDenied("角色无权登记资源占用")
        record = self.repository.get(record_id)
        self._assert_not_in_review(record_id)
        if record["state"] != "approved":
            raise Conflict("只有已批准待动员的任务能登记占用")
        existing = self.repository.find_occupation(record_id)
        if existing is not None and existing["status"] == OCCUPATION_ACTIVE:
            raise Conflict("该任务已确认资源占用")
        fields = self.rules.validate_occupation(data or {})
        if fields["reserve_km"] < float(record["payload"]["required_spare_km"]):
            raise ValidationError("占用公里数不能少于需求%.2f公里" % float(record["payload"]["required_spare_km"]))
        batch = self.repository.get_spare_batch(fields["batch_no"])
        notes: List[str] = []
        shortfall = round(fields["reserve_km"] - batch["remaining_km"], 2)
        if shortfall > 0:
            notes.append("备缆批次%s余量%.2f公里，缺口%.2f公里，已保留草稿待余量补齐" % (batch["batch_no"], batch["remaining_km"], shortfall))
        else:
            shortfall = 0.0
        clash_note = self._window_clash_note(fields, ignore_record_id=record_id)
        if clash_note:
            notes.append(clash_note)
        draft = self.repository.upsert_draft(record_id, fields, batch["id"], shortfall, "；".join(notes), actor.user_id)
        self.audit.note(record_id, actor.user_id, "resource_draft_saved", {"occupation_id": draft["id"], "shortfall_km": shortfall, "notes": "；".join(notes)})
        draft["batch_remaining_km"] = batch["remaining_km"]
        draft["warnings"] = notes
        return draft

    def _window_clash_note(self, fields: Dict[str, Any], ignore_record_id: int) -> str:
        for occ in self.repository.list_occupations(status=OCCUPATION_ACTIVE):
            if occ["record_id"] == ignore_record_id or not windows_overlap(fields["window_start"], fields["window_end"], occ["window_start"], occ["window_end"]):
                continue
            if occ["vessel_name"] == fields["vessel_name"]:
                return "船机%s与任务%s时间窗冲突" % (fields["vessel_name"], occ["reference"])
            if occ["splice_crew"] == fields["splice_crew"]:
                return "接续班组%s与任务%s时间窗冲突" % (fields["splice_crew"], occ["reference"])
        return ""

    def confirm_occupation(self, actor: Actor, occupation_id: int) -> Dict[str, Any]:
        """占用确认：并发提交时事务内只有一票成功，失败者看到最新余量。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_admin_or(actor, DISPATCH_ROLES):
            raise PermissionDenied("角色无权确认资源占用")
        outcome = self.repository.confirm_occupation(occupation_id, actor.user_id)
        record_id = outcome["record_id"]
        self.audit.note(record_id, actor.user_id, "resource_confirmed", {"occupation_id": occupation_id, "vessel_name": outcome["vessel_name"], "splice_crew": outcome["splice_crew"], "batch_no": outcome["batch_no"], "reserve_km": outcome["reserve_km"], "remaining_km": outcome["batch_remaining_km"]})
        return outcome

    def cancel_occupation(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        """取消未动员任务的占用：生效占用退还余量，草稿直接丢弃。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_admin_or(actor, DISPATCH_ROLES):
            raise PermissionDenied("角色无权取消资源占用")
        record = self.repository.get(record_id)
        occupation = self.repository.find_occupation(record_id)
        if occupation is None:
            raise Conflict("该任务没有资源占用")
        if record["state"] not in ("approved",):
            raise Conflict("任务已动员，请使用取消/回港动作释放占用")
        if occupation["status"] == OCCUPATION_DRAFT:
            self.repository.discard_draft(record_id)
            self.audit.note(record_id, actor.user_id, "resource_draft_discarded", {"occupation_id": occupation["id"]})
            return {"record_id": record_id, "status": "draft_discarded"}
        release_info = self.repository.release_occupation(record_id, 0.0)
        self.audit.note(record_id, actor.user_id, "resource_released", {"occupation_id": occupation["id"], "refunded_km": release_info["refunded_km"], "batch_no": release_info["batch_no"], "remaining_km": release_info["batch_remaining_km"]})
        return release_info

    def get_occupation(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        occupation = self.repository.find_occupation(record_id)
        if occupation is None:
            raise NotFound("该任务没有资源占用")
        return occupation

    def list_occupations(self, actor: Actor, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if status and status not in (OCCUPATION_DRAFT, OCCUPATION_ACTIVE, OCCUPATION_RELEASED):
            raise ValidationError("status只能是draft/active/released")
        return self.repository.list_occupations(status=status)

    # ---- 待核对 ---------------------------------------------------------

    def list_reviews(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_open_reviews()

    def resolve_review(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """补齐待核对任务的占用信息后解除冻结。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_admin_or(actor, DISPATCH_ROLES):
            raise PermissionDenied("角色无权核对任务")
        review = self.repository.open_review(record_id)
        if review is None:
            raise Conflict("该任务没有待核对项")
        record = self.repository.get(record_id)
        fields = self.rules.validate_occupation(data or {})
        batch = self.repository.get_spare_batch(fields["batch_no"])
        if fields["reserve_km"] < float(record["payload"].get("required_spare_km", 0)):
            raise ValidationError("占用公里数不能少于任务需求")
        consumed = float(record["payload"].get("spare_used_km", 0)) if record["state"] == "restored" else 0.0
        window_start, window_end = self._migration_window(record, fields["window_start"], fields["window_end"])
        occupation = self.repository.backfill_occupation(
            record_id, fields["vessel_name"], fields["splice_crew"], batch["id"], fields["reserve_km"],
            window_start, window_end, consumed, actor.user_id,
        )
        self.repository.resolve_review(record_id)
        self.audit.note(record_id, actor.user_id, "review_resolved", {"review_id": review["id"], "occupation_id": occupation["id"], "batch_no": batch["batch_no"]})
        occupation["review_resolved"] = True
        return occupation

    # ---- 旧数据迁移 -----------------------------------------------------

    def backfill_legacy_occupations(self, actor: Actor) -> Dict[str, Any]:
        """给已经动员但没有台账的旧任务补回占用；找不到批次的进待核对。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_admin_or(actor, DISPATCH_ROLES):
            raise PermissionDenied("角色无权执行旧数据迁移")
        candidates = self.repository.migration_candidates()
        batches = {b["batch_no"]: b for b in self.repository.list_spare_batches()}
        backfilled: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        for record in candidates:
            payload = record["payload"]
            batch_no = str(payload.get("spare_batch_no", "")).strip()
            vessel = str(payload.get("vessel_name", "")).strip()
            if not batch_no or batch_no not in batches or not vessel:
                review = self.repository.create_review(record["id"], REVIEW_MISSING_BATCH, actor.user_id)
                self.audit.note(record["id"], actor.user_id, "review_created", {"review_id": review["id"], "reason": REVIEW_MISSING_BATCH})
                pending.append({"record_id": record["id"], "reference": record["reference"], "reason": REVIEW_MISSING_BATCH})
                continue
            batch = batches[batch_no]
            reserve_km = float(payload.get("required_spare_km", 0))
            consumed = float(payload.get("spare_used_km", 0))
            window_start, window_end = self._migration_window(record)
            try:
                occupation = self.repository.backfill_occupation(
                    record["id"], vessel, str(payload.get("splice_crew", "待核对班组")), batch["id"],
                    reserve_km, window_start, window_end, consumed, actor.user_id,
                )
            except Conflict as exc:
                review = self.repository.create_review(record["id"], "迁移补登失败：%s" % exc, actor.user_id)
                self.audit.note(record["id"], actor.user_id, "review_created", {"review_id": review["id"], "reason": str(exc)})
                pending.append({"record_id": record["id"], "reference": record["reference"], "reason": str(exc)})
                continue
            self.audit.note(record["id"], actor.user_id, "resource_backfilled", {"occupation_id": occupation["id"], "batch_no": batch_no, "reserve_km": reserve_km, "consumed_km": consumed})
            backfilled.append({"record_id": record["id"], "reference": record["reference"], "occupation_id": occupation["id"], "batch_no": batch_no, "reserve_km": reserve_km, "consumed_km": consumed})
        return {"scanned": len(candidates), "backfilled": backfilled, "pending_review": pending}

    @staticmethod
    def _migration_window(record: Dict[str, Any], fallback_start: str = "", fallback_end: str = ""):
        """旧数据没有时间窗：以动员/创建时间为起点，默认窗口72小时。"""
        payload = record["payload"]
        start = str(payload.get("window_start", "")).strip() or fallback_start
        end = str(payload.get("window_end", "")).strip() or fallback_end
        if start and end:
            return start, end
        base = record["updated_at"]
        start_dt = parse_iso(base)
        return start_dt.isoformat(), (start_dt + timedelta(hours=72)).isoformat()
