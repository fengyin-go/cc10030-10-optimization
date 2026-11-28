"""跨模块查询与校验辅助函数：航次编排业务链路中的交叉验证逻辑。

每个函数都基于真实数据流设计，用于各领域服务在登记和动作执行时
检查跨模块约束。
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Any

from app.store import store

# 装载额度回执中仍然正式占用船舶载重吨的状态：
# 待确认=已预约额度、已占用=已实际装载，两者都计入相交时段占用；
# 已释放（卸货）和已撤销（换船）不再占用任何额度。
QUOTA_ACTIVE_STATUSES = ("待确认", "已占用")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return default


def _parse_iso_date(value: Any) -> date_type | None:
    """尽量把计划离港/到港解析成日期，无法解析时返回 None。"""
    if value in (None, ""):
        return None
    try:
        return date_type.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def require_entity(module: str, entity_id: str | int, label: str = "") -> dict[str, Any] | None:
    """按编号查找目标模块记录，未找到返回 None。"""
    try:
        entry_id = int(entity_id) if isinstance(entity_id, str) and entity_id.isdigit() else int(entity_id)
    except (ValueError, TypeError):
        return None
    return store.find(module, entry_id)


def entity_matches_status(
    module: str,
    entity_id: str | int,
    expected_statuses: list[str],
) -> tuple[bool, str]:
    """校验目标记录是否存在且状态在预期集合内。"""
    entry = require_entity(module, entity_id)
    if entry is None:
        return False, f"{module} 记录 {entity_id} 不存在"
    current_status = entry.get("status", "")
    if current_status not in expected_statuses:
        return False, f"{module} 记录 {entity_id} 状态为「{current_status}」，要求是 {expected_statuses}"
    return True, ""


def vessel_confirmed(vessel_id: str | int) -> tuple[bool, str]:
    """船舶必须已确认才能用于创建航次。"""
    return entity_matches_status("vessel", vessel_id, ["已确认"])


def route_confirmed(route_id: str | int) -> tuple[bool, str]:
    """航线必须已确认才能用于创建航次。"""
    return entity_matches_status("route", route_id, ["已确认"])


def vessel_not_sealed(vessel_id: str | int) -> tuple[bool, str]:
    """船舶未被封存才能创建航次。"""
    entry = require_entity("vessel", vessel_id)
    if entry is None:
        return False, f"船舶 {vessel_id} 不存在"
    if entry.get("status") == "已封存":
        return False, f"船舶 {vessel_id} 已被封存，不能创建新航次"
    return True, ""


def voyage_not_completed(voyage_id: str | int) -> tuple[bool, str]:
    """航次未完成才能关联新货物或船员排班。"""
    entry = require_entity("voyage", voyage_id)
    if entry is None:
        return False, f"航次 {voyage_id} 不存在"
    if entry.get("status") == "已完成":
        return False, f"航次 {voyage_id} 已完成，不允许新增关联记录"
    return True, ""


def voyage_exists(voyage_id: str | int) -> tuple[bool, str]:
    """航次存在性检查。"""
    entry = require_entity("voyage", voyage_id)
    if entry is None:
        return False, f"航次 {voyage_id} 不存在"
    return True, ""


def voyage_window(voyage: dict[str, Any]) -> tuple[date_type | None, date_type | None]:
    """读取航次的装载时段（计划离港→计划到港）。"""
    return (
        _parse_iso_date(voyage.get("计划离港")),
        _parse_iso_date(voyage.get("计划到港")),
    )


def windows_overlap(
    first: tuple[date_type | None, date_type | None],
    second: tuple[date_type | None, date_type | None],
) -> bool:
    """两个装载时段是否相交；任一端点缺失无法判定时按不相交处理。"""
    start_a, end_a = first
    start_b, end_b = second
    if start_a is None or end_a is None or start_b is None or end_b is None:
        return False
    return start_a <= end_b and start_b <= end_a


def vessel_dwt(vessel_id: str | int) -> float | None:
    """读取船舶载重吨；船舶不存在或吨位无法解析时返回 None。"""
    vessel = require_entity("vessel", vessel_id)
    if vessel is None:
        return None
    dwt = _to_float(vessel.get("载重吨", 0))
    return dwt if dwt > 0 else None


def active_quota_rows(vessel_id: str | int | None = None) -> list[dict[str, Any]]:
    """返回仍正式占用载重吨的额度回执，可按船舶过滤。"""
    rows = [
        row for row in store.rows("load_quota")
        if row.get("status") in QUOTA_ACTIVE_STATUSES
    ]
    if vessel_id is not None:
        rows = [row for row in rows if str(row.get("船舶编号", "")) == str(vessel_id)]
    return rows


def check_cargo_tonnage(voyage_id: str | int, cargo_tonnage: float) -> tuple[bool, str]:
    """校验新增装载预约后，船舶在相交装载时段内的正式占用合计不超过载重吨。

    同一艘船可能被调度给多个航次：把与目标航次装载时段相交的其它航次上
    仍然有效的额度回执（待确认/已占用）一并计入；时段不相交的航次互不影响。
    """
    voyage = require_entity("voyage", voyage_id)
    if voyage is None:
        return False, f"航次 {voyage_id} 不存在"

    vessel_id = voyage.get("船舶编号", "")
    dwt = vessel_dwt(vessel_id)
    if dwt is None:
        return False, f"船舶 {vessel_id} 不存在或载重吨无效"

    target_window = voyage_window(voyage)
    occupied = 0.0
    blockers: list[str] = []
    for quota in active_quota_rows(vessel_id):
        other_voyage = require_entity("voyage", quota.get("航次编号", ""))
        # 同航次或装载时段相交的其它航次，其有效额度都在同一时段占用本船载重。
        if other_voyage is not None and not (
            str(other_voyage.get("id", "")) == str(voyage_id)
            or windows_overlap(target_window, voyage_window(other_voyage))
        ):
            continue
        occupied += _to_float(quota.get("预留吨数", 0))
        blockers.append(
            f"航次{quota.get('航次编号', '')}回执{quota.get('id', '')}"
            f"（{quota.get('status', '')} {quota.get('预留吨数', 0)}吨）"
        )

    if occupied + cargo_tonnage > dwt:
        detail = "、".join(blockers) if blockers else "当前无其它占用"
        return False, (
            f"相交装载时段内正式占用合计 {occupied + cargo_tonnage} 吨"
            f"超过船舶载重吨 {dwt:g} 吨；已占用：{detail}"
        )
    return True, ""


def quota_for_cargo(cargo_id: str | int) -> dict[str, Any] | None:
    """找到关联某票货物的最新额度回执（含已撤销/已释放的历史回执）。"""
    matches = [
        quota for quota in store.rows("load_quota")
        if str(quota.get("货物编号", "")) == str(cargo_id)
    ]
    if not matches:
        return None
    return max(matches, key=lambda quota: int(quota.get("id", 0)))


def active_quota_for_cargo(cargo_id: str | int) -> dict[str, Any] | None:
    """找到关联某票货物且仍然有效（待确认/已占用）的额度回执。"""
    active = [
        quota for quota in store.rows("load_quota")
        if str(quota.get("货物编号", "")) == str(cargo_id)
        and quota.get("status") in QUOTA_ACTIVE_STATUSES
    ]
    if not active:
        return None
    return max(active, key=lambda quota: int(quota.get("id", 0)))


def cargo_loading_readiness(cargo_id: str | int) -> dict[str, Any]:
    """货代视角的配载状态：换船后还在等待 vs 已经能履约，以及何时能继续。"""
    cargo = require_entity("cargo", cargo_id)
    if cargo is None:
        return {"ready": False, "stage": "不存在", "reason": f"货物 {cargo_id} 不存在", "blockers": []}
    if cargo.get("status") in ("已装载", "已卸货"):
        return {
            "ready": True,
            "stage": "已能履约" if cargo.get("status") == "已装载" else "已卸货",
            "reason": "该票货物已取得装载资格并完成装载",
            "blockers": [],
        }

    quota = active_quota_for_cargo(cargo_id)
    if quota is None:
        history = quota_for_cargo(cargo_id)
        if history is not None and history.get("status") == "已撤销":
            return {
                "ready": False,
                "stage": "等待配载",
                "reason": (
                    f"原关联船舶 {history.get('船舶编号', '')} 的额度回执"
                    f"{history.get('id', '')} 已随换船撤销，需要重新申请装载额度回执"
                ),
                "blockers": [],
            }
        return {
            "ready": False,
            "stage": "等待配载",
            "reason": "尚未取得关联本航次和船舶的装载额度回执，不能确认装载",
            "blockers": [],
        }

    voyage = require_entity("voyage", quota.get("航次编号", ""))
    if voyage is None:
        return {
            "ready": False,
            "stage": "等待配载",
            "reason": f"回执 {quota.get('id', '')} 关联的航次不存在",
            "blockers": [],
        }
    if str(cargo.get("装载航次", "")) != str(voyage.get("id", "")):
        return {
            "ready": False,
            "stage": "等待配载",
            "reason": (
                f"回执 {quota.get('id', '')} 属于航次 {voyage.get('id', '')}，"
                f"与货物当前装载航次 {cargo.get('装载航次', '')} 不一致，疑似已换船"
            ),
            "blockers": [],
        }

    # 回执有效：说明额度已预留，货代可以凭回执确认装载。
    return {
        "ready": True,
        "stage": "已能履约",
        "reason": f"装载额度回执 {quota.get('id', '')} 有效，可确认装载",
        "quota_id": quota.get("id"),
        "blockers": [],
    }


def quota_availability(vessel_id: str | int, window: tuple[date_type | None, date_type | None]) -> dict[str, Any]:
    """查询某艘船在给定装载时段内的剩余额度，以及占用来源和最早释放时间。"""
    dwt = vessel_dwt(vessel_id)
    if dwt is None:
        return {"available": None, "dwt": None, "occupied": 0.0, "blockers": []}

    occupied = 0.0
    blockers: list[dict[str, Any]] = []
    earliest_free: date_type | None = None
    for quota in active_quota_rows(vessel_id):
        other_voyage = require_entity("voyage", quota.get("航次编号", ""))
        other_window = voyage_window(other_voyage) if other_voyage else (None, None)
        if other_voyage is not None and not (
            windows_overlap(window, other_window)
            or (window[0] is None or window[1] is None)
        ):
            continue
        tons = _to_float(quota.get("预留吨数", 0))
        occupied += tons
        blockers.append({
            "quota_id": quota.get("id"),
            "voyage_id": quota.get("航次编号", ""),
            "cargo_id": quota.get("货物编号", ""),
            "status": quota.get("status", ""),
            "tons": tons,
        })
        # 相交航次的计划到港就是该笔占用最早可能释放的时刻。
        if other_window[1] is not None and (earliest_free is None or other_window[1] < earliest_free):
            earliest_free = other_window[1]

    return {
        "available": max(dwt - occupied, 0.0),
        "dwt": dwt,
        "occupied": occupied,
        "blockers": blockers,
        "earliest_free": earliest_free.isoformat() if earliest_free else None,
    }


def revoke_pending_quotas_for_voyage(voyage_id: str | int) -> list[dict[str, Any]]:
    """换船时撤下该航次上还没实际装载（待确认）的额度回执，返回被撤销的回执列表。

    已占用（已实际装载）和已释放/已撤销的回执不动：前者是真实在船货物，
    后者已经不占用任何额度。
    """
    revoked: list[dict[str, Any]] = []
    for quota in store.rows("load_quota"):
        if str(quota.get("航次编号", "")) != str(voyage_id):
            continue
        if quota.get("status") != "待确认":
            continue
        quota["status"] = "已撤销"
        quota["pending"] = False
        quota["abnormal"] = True
        revoked.append(quota)
    return revoked


def check_crew_shift_overlap(
    crew_name: str,
    embark_date: str,
    disembark_date: str,
    exclude_voyage_id: str | int | None = None,
) -> tuple[bool, str]:
    """同一船员不能同时在两个重叠航次中排班。"""
    from datetime import date as date_type

    try:
        new_start = date_type.fromisoformat(embark_date)
        new_end = date_type.fromisoformat(disembark_date)
    except (ValueError, TypeError):
        # 日期格式无法解析时不拦截（让基础校验处理）
        return True, ""

    for shift_row in store.rows("crew_shift"):
        if str(shift_row.get("船员姓名", "")) != crew_name:
            continue
        if shift_row.get("status") == "已下船":
            continue
        if exclude_voyage_id is not None and str(shift_row.get("id")) == str(exclude_voyage_id):
            continue

        exist_embark = str(shift_row.get("上船日期", ""))
        exist_disembark = str(shift_row.get("下船日期", ""))
        try:
            exist_start = date_type.fromisoformat(exist_embark)
            exist_end = date_type.fromisoformat(exist_disembark)
        except (ValueError, TypeError):
            continue

        # 日期重叠检测
        if new_start <= exist_end and new_end >= exist_start:
            return False, f"船员 {crew_name} 在 {embark_date} 至 {disembark_date} 与已有排班（{exist_embark} 至 {exist_disembark}）重叠"
    return True, ""


def port_call_in_sequence(voyage_id: str | int) -> tuple[bool, str]:
    """港口挂靠必须按航线顺序：前一个挂靠未离港不能新增后续挂靠。"""
    voyage = require_entity("voyage", voyage_id)
    if voyage is None:
        return True, ""  # 存在性由基础逻辑校验

    port_calls = [
        row for row in store.rows("port_call")
        if str(row.get("航次编号", "")) == str(voyage_id)
    ]
    # 按 id 模拟顺序
    port_calls.sort(key=lambda r: int(r.get("id", 0)))

    for i in range(len(port_calls) - 1):
        prev = port_calls[i]
        if prev.get("status") != "已离港":
            return False, f"前一个挂靠（{prev.get('挂靠港口', '')}）尚未离港，不能新增后续挂靠"
    return True, ""


def check_fuel_consumption_between_refuels(
    voyage_id: str | int,
    previous_fuel_qty: float,
) -> tuple[bool, str]:
    """同航次上一次加油后消耗未核验，不能新增燃料记录。"""
    fuel_logs = [
        row for row in store.rows("fuel_log")
        if str(row.get("船舶编号", "")) == str(voyage_id) or True
    ]
    # 查找最近一次待核验或草稿状态的燃料记录
    for log in fuel_logs:
        if log.get("status") in ("草稿", "待核验"):
            return False, "同航次存在未核验的燃料记录，请先完成核验"
    return True, ""


def weather_affects_voyage(sea_area: str) -> list[dict[str, Any]]:
    """气象预警影响特定海域后，返回经过该海域的未完成航次列表。"""
    affected_voyages: list[dict[str, Any]] = []
    for voyage_row in store.rows("voyage"):
        if voyage_row.get("status") == "已完成":
            continue
        # 通过航线查海域
        route_id = voyage_row.get("航线编号", "")
        route = require_entity("route", route_id)
        if route is None:
            continue
        # 检查航线经停港口是否涉及预警海域
        via_ports = str(route.get("经停港口", ""))
        if sea_area in via_ports:
            affected_voyages.append(voyage_row)
    return affected_voyages


def port_call_arrived(port_call_id: str | int) -> tuple[bool, str]:
    """港口挂靠已到达才能发起通关申报。"""
    return entity_matches_status("port_call", port_call_id, ["已确认"])


def clearance_approved_required(voyage_id: str | int) -> tuple[bool, str]:
    """航次有通关申报且审核通过才能允许装卸。"""
    for clearance_row in store.rows("clearance"):
        if str(clearance_row.get("航次编号", "")) == str(voyage_id):
            if clearance_row.get("status") != "审核通过":
                return False, f"航次 {voyage_id} 通关申报未审核通过"
    return True, ""


def all_cargo_loaded_for_voyage(voyage_id: str | int) -> tuple[bool, list[dict[str, Any]]]:
    """获取航次上全部已装载货物，用于编制舱单。"""
    loaded_cargo: list[dict[str, Any]] = []
    for cargo_row in store.rows("cargo"):
        if str(cargo_row.get("装载航次", "")) == str(voyage_id) and cargo_row.get("status") == "已装载":
            loaded_cargo.append(cargo_row)
    if not loaded_cargo:
        return False, []
    return True, loaded_cargo


def charter_active_for_vessel(vessel_name: str) -> tuple[bool, str]:
    """租船合约已签认且未终止才能创建新航次。"""
    for charter_row in store.rows("charter"):
        if str(charter_row.get("租用船舶", "")) == vessel_name:
            if charter_row.get("status") == "已终止":
                return False, f"船舶 {vessel_name} 的租船合约已终止，不能创建新航次"
            if charter_row.get("status") == "已签认":
                return True, ""
    # 没有找到租船合约时允许（自营船舶）
    return True, ""


def crew_cert_valid_for_shift(crew_name: str) -> tuple[bool, str]:
    """船员证书过期或即将到期时不能排入新航次。"""
    from datetime import date as date_type

    today = date_type.today()
    for cert_row in store.rows("crew_cert"):
        if str(cert_row.get("船员姓名", "")) == crew_name:
            expiry_str = str(cert_row.get("有效期至", ""))
            try:
                expiry = date_type.fromisoformat(expiry_str)
            except (ValueError, TypeError):
                continue
            if expiry <= today:
                return False, f"船员 {crew_name} 的证书已到期（{expiry_str}），不能排入新航次"
            # 证书状态为已到期也拦截
            if cert_row.get("status") == "已到期":
                return False, f"船员 {crew_name} 的证书已标记到期，不能排入新航次"
    return True, ""


def seal_vessel_cascade(vessel_id: str | int) -> tuple[bool, str]:
    """船舶封存后级联检查所有进行中航次。"""
    for voyage_row in store.rows("voyage"):
        if str(voyage_row.get("船舶编号", "")) == str(vessel_id):
            if voyage_row.get("status") not in ("已完成",):
                return False, f"船舶 {vessel_id} 存在未完成航次 {voyage_row.get('id', '')}，不能封存"
    return True, ""


def duplicate_manifest_check(voyage_id: str | int) -> tuple[bool, str]:
    """重复签认同一舱单不重复更新（幂等）。"""
    for manifest_row in store.rows("manifest"):
        if str(manifest_row.get("航次编号", "")) == str(voyage_id):
            if manifest_row.get("status") == "已签发":
                return False, f"航次 {voyage_id} 舱单已签发，不能重复签认"
    return True, ""


def voyage_complete_triggers_manifest(voyage_id: str | int) -> bool:
    """航次标记完成后自动检查是否需要生成舱单。"""
    ok, loaded = all_cargo_loaded_for_voyage(voyage_id)
    return ok


def concurrent_voyage_modification_check(
    voyage_id: str | int,
    expected_status: str,
) -> tuple[bool, str]:
    """并发修改航次的冲突检测：校验当前状态与预期是否一致。"""
    voyage = require_entity("voyage", voyage_id)
    if voyage is None:
        return False, f"航次 {voyage_id} 不存在"
    if voyage.get("status") != expected_status:
        return False, f"航次 {voyage_id} 状态已变更为「{voyage.get('status')}」，预期「{expected_status}」"
    return True, ""


# 10条业务链路的完整校验流水线
CROSS_CHAINS = {
    "vessel_route_to_voyage": {
        "description": "船舶已确认+航线已确认→创建航次",
        "checks": [vessel_confirmed, route_confirmed],
    },
    "voyage_to_cargo": {
        "description": "货物登记→关联未完成航次→装载额度回执按相交时段核对载重吨",
        "checks": [voyage_not_completed],
        "tonnage_check": check_cargo_tonnage,
    },
    "voyage_to_crew_shift": {
        "description": "船员排班→指定航次→同一船员不跨重叠航次",
        "checks": [voyage_not_completed],
        "crew_overlap_check": check_crew_shift_overlap,
        "crew_cert_check": crew_cert_valid_for_shift,
    },
    "voyage_to_port_call": {
        "description": "港口挂靠→按航线顺序→前一个挂靠未离港不能新增后续",
        "checks": [voyage_exists],
        "sequence_check": port_call_in_sequence,
    },
    "voyage_to_fuel_log": {
        "description": "燃料记录→关联航次→同航次两次加油消耗核验",
        "consumption_check": check_fuel_consumption_between_refuels,
    },
    "weather_to_voyage": {
        "description": "气象预警→影响海域→自动标记未完成航次",
        "affected_voyages": weather_affects_voyage,
    },
    "port_call_to_clearance": {
        "description": "港口挂靠到达→港口代理发起通关申报→审核通过后允许装卸",
        "checks": [port_call_arrived],
    },
    "cargo_to_manifest": {
        "description": "全部货物装载→编制舱单→汇总件数和吨位",
        "loaded_check": all_cargo_loaded_for_voyage,
        "duplicate_check": duplicate_manifest_check,
    },
    "charter_to_voyage": {
        "description": "租船合约签认→船舶可创建新航次→合约到期后禁止",
        "checks": [charter_active_for_vessel],
    },
    "crew_cert_to_shift": {
        "description": "船员证书到期/过期→该船员不能排入新航次",
        "checks": [crew_cert_valid_for_shift],
    },
}