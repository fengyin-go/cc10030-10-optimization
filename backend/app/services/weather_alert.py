"""气象预警领域服务：跨模块业务链路（预警→海域→航次）校验。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cross_module import weather_affects_voyage
from app.store import store


class WeatherAlertService(BaseModuleService):
    MODULE = "weather_alert"
    ENTITY = "预警通知"
    OPERATION_TERM = "签收预警"
    LIST_FIELDS = ["预警海域", "预警类型", "预警等级", "生效时间", "预计持续", "影响航线", "签发单位", "备注说明"]
    REQUIRED_FIELDS = ["预警海域", "预警类型", "预警等级"]
    STATUS_ORDER = ["草稿", "待签收", "已签收", "已解除"]
    ACTION_RULES = {"签收预警": "已签收", "调整航次": "已签收", "解除预警": "已解除"}
    NEGATIVE_ACTIONS = []

    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        result, msg = super().run_action(entry_id, action, remark)
        # 签收预警时自动标记受影响航次
        if result is not None and action == "签收预警":
            alert = store.find(self.MODULE, entry_id)
            if alert:
                sea_area = alert.get("预警海域", "")
                affected = weather_affects_voyage(sea_area)
                if affected:
                    voyage_ids = [str(v.get("id", "")) for v in affected]
                    msg = f"预警已签收，影响航次：{', '.join(voyage_ids)}"
        return result, msg
