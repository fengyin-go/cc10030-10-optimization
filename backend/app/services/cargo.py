"""货物登记领域服务：跨模块业务链路（航次→货物、货物→舱单、装载额度回执→装载确认）校验。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cross_module import (
    active_quota_for_cargo,
    cargo_loading_readiness,
    check_cargo_tonnage,
    quota_for_cargo,
    require_entity,
    voyage_not_completed,
    _to_float,
)
from app.services.load_quota import LoadQuotaService
from app.store import store


class CargoService(BaseModuleService):
    MODULE = "cargo"
    ENTITY = "货物"
    OPERATION_TERM = "确认装载"
    LIST_FIELDS = ["货物品名", "托运方", "件数", "总吨数", "装载航次", "装货港", "卸货港", "额度回执", "备注说明"]
    REQUIRED_FIELDS = ["货物品名", "托运方", "件数"]
    STATUS_ORDER = ["草稿", "待装载", "已装载", "已卸货"]
    ACTION_RULES = {"确认装载": "已装载", "调整数量": "已装载", "确认卸货": "已卸货"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]] | tuple[dict[str, Any] | None, str]:
        # 跨模块校验必须在创建条目之前执行，避免自身条目被重复计入。
        # 货物登记本身不再直接卡载重吨：载重阈值在申请装载额度回执时按
        # 相交装载时段核对，登记后未拿到回执的货物处于「等待配载」。

        # 跨模块校验：装载航次必须存在且未完成
        voyage_id = values.get("装载航次", "")
        if voyage_id:
            ok, msg = voyage_not_completed(voyage_id)
            if not ok:
                return None, f"货物登记失败：{msg}"

        # 基础必填校验和创建
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["额度回执"] = ""
        return entry, []

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        items, total = super().list_entries(**kwargs)
        for row in items:
            self._annotate_readiness(row)
        return items, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is not None:
            self._annotate_readiness(entry)
        return entry

    def _annotate_readiness(self, row: dict[str, Any]) -> None:
        """补注配载状态，帮助调度员区分换船后还在等待的配载和已经能履约的配载。"""
        readiness = cargo_loading_readiness(row.get("id", -1))
        row["配载状态"] = readiness["stage"]
        row["配载说明"] = readiness["reason"]
        quota = active_quota_for_cargo(row.get("id", -1))
        if quota is not None and not row.get("额度回执"):
            row["额度回执"] = quota.get("id", "")

    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "确认装载":
            return self._confirm_loading(entry_id, entry)
        if action == "调整数量":
            return self._adjust_quantity(entry_id, entry, remark)
        if action == "确认卸货":
            return self._confirm_discharge(entry_id, entry)
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    def _confirm_loading(self, entry_id: int, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        # 幂等：同一张装载额度回执重复确认时只占一次。
        if entry.get("status") == "已装载":
            quota = quota_for_cargo(entry_id)
            quota_id = quota.get("id", "") if quota else entry.get("额度回执", "")
            return entry, f"货物已装载，回执 {quota_id} 仅占用一次，无需重复确认"

        # 确认装载必须消费关联本航次和船舶的装载额度回执
        quota = active_quota_for_cargo(entry_id)
        if quota is None:
            history = quota_for_cargo(entry_id)
            if history is not None and history.get("status") == "已撤销":
                return None, (
                    f"确认装载失败：原船舶 {history.get('船舶编号', '')} 的回执"
                    f"{history.get('id', '')} 已撤销，请按当前航次重新申请装载额度回执"
                )
            return None, "确认装载失败：缺少关联本航次和船舶的有效装载额度回执"

        voyage = require_entity("voyage", quota.get("航次编号", ""))
        if voyage is None:
            return None, f"确认装载失败：回执 {quota.get('id', '')} 关联航次不存在"
        if str(entry.get("装载航次", "")) != str(voyage.get("id", "")):
            return None, (
                f"确认装载失败：回执 {quota.get('id', '')} 属于航次 {voyage.get('id', '')}，"
                f"货物当前挂在航次 {entry.get('装载航次', '')}，凭证与航次不匹配"
            )
        if str(quota.get("船舶编号", "")) != str(voyage.get("船舶编号", "")):
            return None, (
                f"确认装载失败：回执 {quota.get('id', '')} 是旧船 "
                f"{quota.get('船舶编号', '')} 的凭证，当前船舶为 {voyage.get('船舶编号', '')}，拒绝装载"
            )

        # 消费回执：待确认→已占用，同一回执重复确认不会再次占用
        result, message = LoadQuotaService().run_action(quota["id"], "确认占用")
        if result is None:
            return None, f"确认装载失败：{message}"

        loaded, loaded_msg = super().run_action(entry_id, "确认装载")
        if loaded is None:
            return None, loaded_msg
        loaded["额度回执"] = quota["id"]
        return loaded, f"货物已确认装载，已消费装载额度回执 {quota['id']}（{quota.get('预留吨数', 0)}吨）"

    def _adjust_quantity(self, entry_id: int, entry: dict[str, Any], remark: str | None) -> tuple[dict[str, Any] | None, str]:
        """调整数量仍需持有有效回执，并按新吨位重新核对相交时段载重阈值。"""
        try:
            new_tonnage = float(str(remark).strip())
        except (ValueError, TypeError):
            return None, "调整数量失败：请在备注中提供调整后的总吨数"
        if new_tonnage <= 0:
            return None, "调整数量失败：调整后的总吨数必须大于 0"
        quota = active_quota_for_cargo(entry_id)
        if quota is None:
            return None, "调整数量失败：缺少有效装载额度回执，不能调整已装载配载"
        # 以“新吨位减去本回执已占用吨位”作为增量重新核对
        delta = new_tonnage - _to_float(quota.get("预留吨数", 0))
        if delta > 0:
            ok, msg = check_cargo_tonnage(entry.get("装载航次", ""), delta)
            if not ok:
                return None, f"调整数量失败：{msg}"
        result, message = super().run_action(entry_id, "调整数量")
        if result is not None:
            quota["预留吨数"] = new_tonnage
            result["总吨数"] = new_tonnage
        return result, message

    def _confirm_discharge(self, entry_id: int, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") != "已装载":
            return None, f"确认卸货失败：货物当前状态为「{entry.get('status', '')}」，只有已装载货物可以卸货"

        quota = quota_for_cargo(entry_id)
        if quota is not None and quota.get("status") == "已占用":
            released, released_msg = LoadQuotaService().run_action(quota["id"], "确认释放")
            if released is None:
                return None, f"确认卸货失败：{released_msg}"

        result, message = super().run_action(entry_id, "确认卸货")
        if result is not None and quota is not None:
            message = f"货物已卸货，装载额度回执 {quota['id']} 已释放，吨位可用于不重合的后续航次"
        return result, message
