"""装载额度回执领域服务。

货代在确认装载前，必须先取得一张关联具体航次、船舶和货物的装载额度回执。
回执在签发时即按预留吨数占用船舶在相交装载时段内的载重额度：

- 待确认：已预约额度，货物尚未实际装载，换船时会被撤下；
- 已占用：货物登记确认装载时消费回执，正式占用船舶载重吨；
- 已释放：货物卸货后，吨位释放给不重合的后续航次；
- 已撤销：换船等原因作废，不再占用额度，也不能再用于确认装载。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cross_module import (
    QUOTA_ACTIVE_STATUSES,
    active_quota_for_cargo,
    check_cargo_tonnage,
    require_entity,
    vessel_confirmed,
    _to_float,
)
from app.store import store


class LoadQuotaService(BaseModuleService):
    MODULE = "load_quota"
    ENTITY = "装载额度回执"
    OPERATION_TERM = "签发回执"
    LIST_FIELDS = [
        "船舶编号", "航次编号", "货物编号", "预留吨数",
        "申请货代", "额度来源", "备注说明",
    ]
    REQUIRED_FIELDS = ["船舶编号", "航次编号", "货物编号", "预留吨数"]
    STATUS_ORDER = ["待确认", "已占用", "已释放", "已撤销"]
    ACTION_RULES = {
        "确认占用": "已占用",
        "确认释放": "已释放",
        "撤下回执": "已撤销",
    }
    NEGATIVE_ACTIONS = ["撤下回执"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]] | tuple[dict[str, Any] | None, str]:
        # 跨模块校验放在基础登记之前，失败时不产生占位回执。
        vessel_id = values.get("船舶编号", "")
        voyage_id = values.get("航次编号", "")
        cargo_id = values.get("货物编号", "")
        tons = _to_float(values.get("预留吨数", 0))

        if tons <= 0:
            return None, "装载额度回执签发失败：预留吨数必须大于 0"

        # 船舶必须已确认
        ok, msg = vessel_confirmed(vessel_id)
        if not ok:
            return None, f"装载额度回执签发失败：{msg}"

        # 航次必须存在、未完成，且当前就挂在申请的船舶上
        voyage = require_entity("voyage", voyage_id)
        if voyage is None:
            return None, f"装载额度回执签发失败：航次 {voyage_id} 不存在"
        if voyage.get("status") == "已完成":
            return None, f"装载额度回执签发失败：航次 {voyage_id} 已完成，不再受理装载预约"
        if str(voyage.get("船舶编号", "")) != str(vessel_id):
            return None, (
                f"装载额度回执签发失败：航次 {voyage_id} 当前船舶为"
                f"{voyage.get('船舶编号', '')}，与申请船舶 {vessel_id} 不一致"
            )

        # 货物必须存在、登记在同一航次上，且尚未装载/卸货
        cargo = require_entity("cargo", cargo_id)
        if cargo is None:
            return None, f"装载额度回执签发失败：货物 {cargo_id} 不存在"
        if str(cargo.get("装载航次", "")) != str(voyage_id):
            return None, (
                f"装载额度回执签发失败：货物 {cargo_id} 登记的装载航次为"
                f"{cargo.get('装载航次', '')}，与回执航次 {voyage_id} 不一致"
            )
        if cargo.get("status") in ("已装载", "已卸货"):
            return None, f"装载额度回执签发失败：货物 {cargo_id} 已完成装载，无需重复申请"

        # 同一票货物只能持有一张有效回执，避免重复预约额度
        existing = active_quota_for_cargo(cargo_id)
        if existing is not None:
            return None, (
                f"装载额度回执签发失败：货物 {cargo_id} 已持有有效回执"
                f"{existing.get('id', '')}（{existing.get('status', '')}）"
            )

        # 相交装载时段内的正式占用合计不能超过船舶载重吨
        ok, msg = check_cargo_tonnage(voyage_id, tons)
        if not ok:
            return None, f"装载额度回执签发失败：{msg}"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry.setdefault("额度来源", "货代预约")
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        current = entry.get("status", "")
        if action == "确认占用":
            if current == "已占用":
                return entry, "回执已占用，重复确认不重复占用载重额度"
            if current != "待确认":
                return None, f"确认占用失败：回执状态为「{current}」，只有待确认回执可以占用"
        elif action == "确认释放":
            if current not in ("待确认", "已占用"):
                return None, f"确认释放失败：回执状态为「{current}」，有效回执才能释放"
        elif action == "撤下回执":
            if current not in ("待确认",):
                return None, f"撤下回执失败：回执状态为「{current}」，只有尚未实际装载的待确认回执可以撤下"
        else:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["active_tonnage"] = sum(
            _to_float(row.get("预留吨数", 0))
            for row in store.rows(self.MODULE)
            if row.get("status") in QUOTA_ACTIVE_STATUSES
        )
        return snapshot
