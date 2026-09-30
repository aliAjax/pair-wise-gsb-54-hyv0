"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Tuple

from .domain import Conflict, ValidationError, boolean, integer, number, text


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {
    'approve': {'repair_manager'},
    'mobilize': {'vessel_master'},
    'survey': {'cable_engineer'},
    'splice': {'cable_engineer'},
    'test': {'noc_operator'},
    'restore': {'noc_operator', 'repair_manager'},
    'cancel': {'repair_manager'},
    'return_port': {'vessel_master', 'dispatcher'},
    'splice_fail': {'cable_engineer'},
}
TRANSITIONS = {
    'approve': {'detected': 'approved'},
    'mobilize': {'approved': 'mobilized'},
    'survey': {'mobilized': 'surveyed'},
    'splice': {'surveyed': 'spliced'},
    'test': {'spliced': 'tested'},
    'restore': {'tested': 'restored'},
    'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'},
    'return_port': {'mobilized': 'returned', 'surveyed': 'returned'},
    'splice_fail': {'surveyed': 'splice_failed'},
}
# 进入这些状态后占用即终止，未消耗的船机/班组/备缆全部释放。
RELEASE_ACTIONS = {'cancel', 'return_port', 'splice_fail'}
# 接续成功：备缆按实际消耗结算，余量返还。
CONSUME_ACTIONS = {'splice'}

# 占用台账状态
OCC_DRAFT = "draft"
OCC_CONFIRMED = "confirmed"
OCC_CONSUMED = "consumed"
OCC_RELEASED = "released"
OCC_DISCARDED = "discarded"

# 旧数据迁移核对状态
RECONCILE_OK = "ok"
RECONCILE_PENDING = "pending_batch"

OCCUPATION_ROLES = {'dispatcher'}


def parse_window(value: Any, key: str = "window") -> str:
    """解析时间窗边界并归一化为UTC秒级ISO串，便于字典序比较。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是ISO时间文本" % key)
    raw = value.strip()
    normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("%s必须是ISO时间文本" % key) from exc
    if parsed.tzinfo is None:
        raise ValidationError("%s必须带时区" % key)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(OCCUPATION_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or action in ACTION_ROLES and role in ACTION_ROLES[action]

    def role_can_manage_ledger(self, role: str) -> bool:
        return role == "admin" or role in OCCUPATION_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled", "returned", "splice_failed"} or item.get("reconcile_status") == RECONCILE_PENDING:
                continue
            if item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def validate_occupation(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """动员前占用登记参数：船机、接续班组、备缆批次、占用公里数与时间窗。"""
        data = data or {}
        vessel = text(data, "vessel_code")
        crew = text(data, "crew_code")
        batch_no = text(data, "batch_no")
        reserve_km = number(data, "reserve_km", 0.01)
        window_start = parse_window(data.get("window_start"), "window_start")
        window_end = parse_window(data.get("window_end"), "window_end")
        if window_end <= window_start:
            raise ValidationError("时间窗结束时间必须晚于开始时间")
        return {
            "vessel_code": vessel,
            "crew_code": crew,
            "batch_no": batch_no,
            "reserve_km": round(reserve_km, 3),
            "window_start": window_start,
            "window_end": window_end,
            "note": str(data.get("note") or "").strip(),
        }

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            # 船机与备缆余量由占用台账确认后锁定，动员时以占用登记为准。
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员，资源占用生效"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            used = number(data, "spare_used_km", 0)
            if used < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = used
            summary = "光缆接续完成，剩余备缆余量已返还"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            changes["release_note"] = "取消后资源占用已全部释放"
            summary = "抢修取消，资源占用已释放"
        elif action == "return_port":
            changes["return_reason"] = text(data, "reason")
            changes["release_note"] = "回港后资源占用已全部释放"
            summary = "船舶回港，资源占用已释放"
        elif action == "splice_fail":
            changes["splice_fail_reason"] = text(data, "reason")
            changes["release_note"] = "接续失败，未消耗占用已释放"
            summary = "接续失败，资源占用已释放"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
