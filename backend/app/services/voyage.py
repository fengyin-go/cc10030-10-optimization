"""航次编排领域服务：跨模块业务链路（船舶+航线→航次、租船合约→航次、航次→货物/舱单）校验。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cross_module import (
    charter_active_for_vessel,
    require_entity,
    revoke_pending_quotas_for_voyage,
    route_confirmed,
    vessel_confirmed,
    vessel_dwt,
    voyage_window,
    windows_overlap,
    active_quota_rows,
    _to_float,
)


class VoyageService(BaseModuleService):
    MODULE = "voyage"
    ENTITY = "航次"
    OPERATION_TERM = "签认航次"
    LIST_FIELDS = ["船舶编号", "航线编号", "计划离港", "计划到港", "实际离港", "实际到港", "航次状态", "备注说明"]
    REQUIRED_FIELDS = ["船舶编号", "航线编号", "计划离港"]
    STATUS_ORDER = ["草稿", "待签认", "已签认", "已完成"]
    # 调整船舶不改变航次自身状态，只换挂靠船舶并处理旧船额度回执。
    ACTION_RULES = {"签认航次": "已签认", "推迟离港": "待签认", "标记完成": "已完成", "调整船舶": ""}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]] | tuple[dict[str, Any] | None, str]:
        # 基础必填校验
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 跨模块校验：船舶必须已确认
        vessel_id = values.get("船舶编号", "")
        ok, msg = vessel_confirmed(vessel_id)
        if not ok:
            return None, f"创建航次失败：{msg}"

        # 跨模块校验：航线必须已确认
        route_id = values.get("航线编号", "")
        ok, msg = route_confirmed(route_id)
        if not ok:
            return None, f"创建航次失败：{msg}"

        # 跨模块校验：租船合约必须有效
        ok, msg = charter_active_for_vessel(vessel_id)
        if not ok:
            return None, f"创建航次失败：{msg}"

        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action == "调整船舶":
            return self._switch_vessel(entry_id, remark)

        # 标记完成时触发舱单待签发
        result, msg = super().run_action(entry_id, action)
        if result is not None and action == "标记完成":
            from app.services.cross_module import voyage_complete_triggers_manifest
            from app.store import store
            voyage = store.find("voyage", entry_id)
            if voyage and voyage_complete_triggers_manifest(entry_id):
                msg = f"航次已完成，请关注舱单编制"
        return result, msg

    def _switch_vessel(self, entry_id: int, new_vessel_id: str | None) -> tuple[dict[str, Any] | None, str]:
        """调度员把航次换到另一条船。

        - 新船必须已确认且租船合约有效；
        - 旧船上还没实际装载（待确认）的额度回执必须明确撤下，已装载/已释放的保留；
        - 撤下后按新船的载重吨和相交装载时段重新核对剩余配载能否履约。
        """
        from app.store import store

        voyage = store.find("voyage", entry_id)
        if voyage is None:
            return None, f"航次 {entry_id} 不存在或已归档"
        if not str(new_vessel_id or "").strip():
            return None, "调整船舶失败：请在备注中提供新的船舶编号"
        new_vessel_id = str(new_vessel_id).strip()
        old_vessel_id = str(voyage.get("船舶编号", ""))
        if new_vessel_id == old_vessel_id:
            return None, f"调整船舶失败：航次 {entry_id} 当前已挂在船舶 {old_vessel_id} 上"

        ok, msg = vessel_confirmed(new_vessel_id)
        if not ok:
            return None, f"调整船舶失败：{msg}"
        ok, msg = charter_active_for_vessel(new_vessel_id)
        if not ok:
            return None, f"调整船舶失败：{msg}"

        # 先撤下旧船额度：还没实际装载的待确认回执一律撤销。
        revoked = revoke_pending_quotas_for_voyage(entry_id)
        # 已实际装载的货物不允许随航次整体换船，避免载重账实不符。
        loaded_cargo = [
            row for row in store.rows("cargo")
            if str(row.get("装载航次", "")) == str(entry_id) and row.get("status") == "已装载"
        ]
        if loaded_cargo:
            return None, (
                f"调整船舶失败：航次 {entry_id} 已有 {len(loaded_cargo)} 票货物实际装载，"
                "须先卸货再换船"
            )

        # 换船前先按新船核对：本航次等待配载的货物吨位 + 新船相交航次的现有占用。
        dwt = vessel_dwt(new_vessel_id)
        if dwt is None:
            return None, f"调整船舶失败：新船 {new_vessel_id} 载重吨无效"
        window = voyage_window(voyage)
        waiting_tonnage = sum(
            _to_float(row.get("总吨数", 0))
            for row in store.rows("cargo")
            if str(row.get("装载航次", "")) == str(entry_id)
            and row.get("status") not in ("已装载", "已卸货")
        )
        occupied = waiting_tonnage
        for quota in active_quota_rows(new_vessel_id):
            other = require_entity("voyage", quota.get("航次编号", ""))
            if other is not None and not (
                str(other.get("id", "")) == str(entry_id)
                or windows_overlap(window, voyage_window(other))
            ):
                continue
            occupied += _to_float(quota.get("预留吨数", 0))
        if occupied > dwt:
            return None, (
                f"调整船舶失败：新船 {new_vessel_id} 在相交装载时段需占用 "
                f"{occupied:g} 吨，超过载重吨 {dwt:g} 吨，航次仍保留在船舶 {old_vessel_id}"
            )

        voyage["船舶编号"] = new_vessel_id
        revoked_ids = "、".join(str(q.get("id", "")) for q in revoked) or "无"
        return voyage, (
            f"航次 {entry_id} 已由船舶 {old_vessel_id} 调整至 {new_vessel_id}；"
            f"旧船待确认回执已撤下（{revoked_ids}），相关货物需重新申请新船额度回执"
        )
