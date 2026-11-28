"""重叠装载时段载重冲突与装载额度回执全链路测试。

场景原型：船舶 S4 载重 100 吨，两趟航次换船后装载时段重合，
两名货代分别申请 60 吨和 50 吨的装载额度回执并确认装载。
覆盖：相交时段阈值核对、回执消费与幂等、卸货释放、换船撤单、旧船凭证拒绝。
"""
from __future__ import annotations

import pytest

from app.services.cargo import CargoService
from app.services.load_quota import LoadQuotaService
from app.services.vessel import VesselService
from app.services.voyage import VoyageService
from app.services.cross_module import (
    cargo_loading_readiness,
    quota_availability,
    quota_for_cargo,
    revoke_pending_quotas_for_voyage,
    voyage_window,
)
from app.store import store


@pytest.fixture()
def s4_context():
    """构造载重 100 吨的 S4 以及两艘其它船舶，返回各服务与编号。"""
    vessel_svc = VesselService()
    s4 = vessel_svc.create_entry({"船名": "S4", "总吨位": "200", "载重吨": "100"})[0]["id"]
    vessel_svc.run_action(s4, "确认档案")
    s3 = vessel_svc.create_entry({"船名": "S3", "总吨位": "300", "载重吨": "300"})[0]["id"]
    vessel_svc.run_action(s3, "确认档案")
    s5 = vessel_svc.create_entry({"船名": "S5", "总吨位": "150", "载重吨": "80"})[0]["id"]
    vessel_svc.run_action(s5, "确认档案")

    voyage_svc = VoyageService()
    cargo_svc = CargoService()
    quota_svc = LoadQuotaService()
    return {
        "vessel_svc": vessel_svc,
        "voyage_svc": voyage_svc,
        "cargo_svc": cargo_svc,
        "quota_svc": quota_svc,
        "s4": s4,
        "s3": s3,
        "s5": s5,
    }


def _voyage(svc, vessel_id, start, end):
    entry, result = svc.create_entry({
        "船舶编号": vessel_id,
        "航线编号": 3,
        "计划离港": start,
        "计划到港": end,
    })
    assert entry is not None, f"航次创建失败: {result}"
    return entry["id"]


def _cargo(svc, voyage_id, name, tons):
    entry, result = svc.create_entry({
        "货物品名": name,
        "托运方": f"货代-{name}",
        "件数": "10",
        "总吨数": str(tons),
        "装载航次": voyage_id,
    })
    assert entry is not None, f"货物登记失败: {result}"
    return entry["id"]


def _quota(svc, vessel_id, voyage_id, cargo_id, tons):
    return svc.create_entry({
        "船舶编号": vessel_id,
        "航次编号": voyage_id,
        "货物编号": cargo_id,
        "预留吨数": str(tons),
        "申请货代": "货代",
    })


class TestOverlappingWindowQuota:
    """把原本错开的两段时间改成重叠，并分别确认 60 吨和 50 吨。"""

    def test_overlapping_windows_second_quota_rejected(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]

        # 两趟航次都挂在 S4，装载时段重合
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        v2 = _voyage(svc, s4, "2026-12-05", "2026-12-15")
        c1 = _cargo(csvc, v1, "钢材60", 60)
        c2 = _cargo(csvc, v2, "设备50", 50)

        q1, msg1 = _quota(qsvc, s4, v1, c1, 60)
        assert q1 is not None, f"第一票 60 吨在 100 吨阈值内应通过: {msg1}"

        # 相交时段正式占用 60 + 50 = 110 > 100，第二票回执被拒
        q2, msg2 = _quota(qsvc, s4, v2, c2, 50)
        assert q2 is None, "相交时段合计 110 吨超过 100 吨，必须拦截"
        assert "110" in msg2 and "100" in msg2

        # 第二票仍处于等待配载，货代可以查清何时能继续
        readiness = cargo_loading_readiness(c2)
        assert readiness["ready"] is False and readiness["stage"] == "等待配载"
        availability = quota_availability(s4, voyage_window(store.find("voyage", v2)))
        assert availability["available"] == 40
        assert availability["earliest_free"] == "2026-12-10"

        # 没有回执不能确认装载
        loaded, msg = csvc.run_action(c2, "确认装载")
        assert loaded is None and "装载额度回执" in msg

    def test_non_overlapping_windows_share_full_capacity(self, s4_context):
        """不重合的下一趟可以重新使用整船额度。"""
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]

        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-05")
        v2 = _voyage(svc, s4, "2026-12-06", "2026-12-12")
        c1 = _cargo(csvc, v1, "钢材90", 90)
        c2 = _cargo(csvc, v2, "谷物90", 90)

        q1, _ = _quota(qsvc, s4, v1, c1, 90)
        assert q1 is not None
        # 时段不重合，第二趟 90 吨不与第一趟叠加，仍然满足 100 吨阈值
        q2, msg2 = _quota(qsvc, s4, v2, c2, 90)
        assert q2 is not None, f"不重合航次应独立核算载重: {msg2}"

    def test_confirm_loading_consumes_quota_and_is_idempotent(self, s4_context):
        """确认装载消费关联本航次和船舶的回执；重复确认只占一次。"""
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]

        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "钢材60", 60)
        q1, _ = _quota(qsvc, s4, v1, c1, 60)
        quota_id = q1["id"]

        loaded, msg = csvc.run_action(c1, "确认装载")
        assert loaded is not None, msg
        assert loaded["status"] == "已装载"
        assert loaded["额度回执"] == quota_id
        assert store.find("load_quota", quota_id)["status"] == "已占用"

        # 同一张回执重复确认：幂等成功，不重复占用
        again, msg_again = csvc.run_action(c1, "确认装载")
        assert again is not None and "仅占用一次" in msg_again
        # 该回执只有一笔 60 吨占用，没有因重复确认翻倍
        receipt = store.find("load_quota", quota_id)
        assert receipt["status"] == "已占用"
        assert float(receipt["预留吨数"]) == 60
        assert sum(
            1 for q in store.rows("load_quota")
            if str(q.get("货物编号", "")) == str(c1)
        ) == 1

    def test_discharge_releases_tonnage_for_next_voyage(self, s4_context):
        """卸下已取得资格的一票后，前一趟吨位释放给不重合的下一趟。"""
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]

        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-05")
        v2 = _voyage(svc, s4, "2026-12-06", "2026-12-12")
        c1 = _cargo(csvc, v1, "钢材60", 60)
        c2 = _cargo(csvc, v2, "设备50", 50)
        q1, _ = _quota(qsvc, s4, v1, c1, 60)
        csvc.run_action(c1, "确认装载")

        # 卸货前：下一票回执也可签发（窗口本不重合），这里验证卸货后回执释放
        csvc.run_action(c1, "确认卸货")
        assert store.find("load_quota", q1["id"])["status"] == "已释放"
        assert store.find("cargo", c1)["status"] == "已卸货"

        q2, msg = _quota(qsvc, s4, v2, c2, 50)
        assert q2 is not None, f"释放后下一趟应能取得额度: {msg}"
        availability = quota_availability(s4, voyage_window(store.find("voyage", v2)))
        assert availability["available"] == 50  # 100 - 50，已释放回执不占用

    def test_overlapping_frees_after_discharge_then_waiting_cargo_can_continue(self, s4_context):
        """重合场景：先到的一票卸货释放后，等待的一票即可取得回执继续。"""
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]

        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        v2 = _voyage(svc, s4, "2026-12-05", "2026-12-15")
        c1 = _cargo(csvc, v1, "钢材60", 60)
        c2 = _cargo(csvc, v2, "设备50", 50)
        q1, _ = _quota(qsvc, s4, v1, c1, 60)
        csvc.run_action(c1, "确认装载")

        # 60 已占用，50 申请在重合窗口被拒
        q2_before, _ = _quota(qsvc, s4, v2, c2, 50)
        assert q2_before is None

        # 第一票卸货，额度释放
        csvc.run_action(c1, "确认卸货")
        q2_after, msg = _quota(qsvc, s4, v2, c2, 50)
        assert q2_after is not None, f"卸货释放后等待的一票应能继续: {msg}"
        loaded, msg = csvc.run_action(c2, "确认装载")
        assert loaded is not None, msg


class TestVesselSwitch:
    """换船：撤下未实际装载的旧船回执，旧船凭证确认必须拒绝。"""

    def test_switch_revokes_pending_quota_and_blocks_old_vessel_receipt(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s3, s4 = ctx["s3"], ctx["s4"]

        # 航次最初挂在旧船 S3，港代已用旧船额度受理一票（待确认，未实际装载）
        v1 = _voyage(svc, s3, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "旧船货60", 60)
        old_quota, _ = _quota(qsvc, s3, v1, c1, 60)
        old_quota_id = old_quota["id"]
        assert store.find("load_quota", old_quota_id)["status"] == "待确认"

        # 调度员把航次换到 S4：待确认回执必须明确撤下
        switched, msg = svc.run_action(v1, "调整船舶", str(s4))
        assert switched is not None, msg
        assert str(switched["船舶编号"]) == str(s4)
        assert store.find("load_quota", old_quota_id)["status"] == "已撤销"

        # 货物变为等待配载，提示重新申请
        readiness = cargo_loading_readiness(c1)
        assert readiness["ready"] is False and readiness["stage"] == "等待配载"
        assert "撤销" in readiness["reason"]

        # 用旧船凭证确认同一张票：货物登记拒绝
        loaded, msg = csvc.run_action(c1, "确认装载")
        assert loaded is None and "回执" in msg

        # 旧船 S3 的后续作业仍可使用已释放的额度（撤销即不占用）
        v_other = _voyage(svc, s3, "2026-12-02", "2026-12-08")
        c_other = _cargo(csvc, v_other, "S3后续货100", 100)
        q_other, msg = _quota(qsvc, s3, v_other, c_other, 100)
        assert q_other is not None, f"旧船已释放额度应可继续受理: {msg}"

    def test_switch_then_new_vessel_receipt_allows_loading(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s3, s4 = ctx["s3"], ctx["s4"]

        v1 = _voyage(svc, s3, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "换船货60", 60)
        old_quota, _ = _quota(qsvc, s3, v1, c1, 60)

        switched, _ = svc.run_action(v1, "调整船舶", str(s4))
        assert switched is not None
        # 重新申请新船 S4 的回执并确认装载
        new_quota, msg = _quota(qsvc, s4, v1, c1, 60)
        assert new_quota is not None, f"换船后应能申请新船回执: {msg}"
        loaded, msg = csvc.run_action(c1, "确认装载")
        assert loaded is not None and loaded["额度回执"] == new_quota["id"], msg
        # 旧回执始终保持已撤销，没有被重复启用
        assert store.find("load_quota", old_quota["id"])["status"] == "已撤销"

    def test_switch_with_loaded_cargo_rejected(self, s4_context):
        """已实际装载的航次不允许整体换船。"""
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s3, s4 = ctx["s3"], ctx["s4"]

        v1 = _voyage(svc, s3, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "在船货60", 60)
        quota, _ = _quota(qsvc, s3, v1, c1, 60)
        csvc.run_action(c1, "确认装载")

        switched, msg = svc.run_action(v1, "调整船舶", str(s4))
        assert switched is None and "实际装载" in msg
        # 已占用回执不受影响
        assert store.find("load_quota", quota["id"])["status"] == "已占用"

    def test_switch_rechecks_new_vessel_overlap_capacity(self, s4_context):
        """换船后按新船的相交时段重新核对载重阈值。"""
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s3, s4, s5 = ctx["s3"], ctx["s4"], ctx["s5"]

        # S4 上已有一趟重合航次占用 70 吨
        v_existing = _voyage(svc, s4, "2026-12-01", "2026-12-12")
        c_existing = _cargo(csvc, v_existing, "S4既有70", 70)
        _quota(qsvc, s4, v_existing, c_existing, 70)

        # 待换船航次在 S3 上有 60 吨待确认回执
        v1 = _voyage(svc, s3, "2026-12-03", "2026-12-10")
        c1 = _cargo(csvc, v1, "换船货60", 60)
        _quota(qsvc, s3, v1, c1, 60)

        # 换到 S4：70 + 60 = 130 > 100，拒绝
        switched, msg = svc.run_action(v1, "调整船舶", str(s4))
        assert switched is None and "超过载重吨" in msg
        # 换船失败时航次仍挂在 S3
        assert store.find("voyage", v1)["船舶编号"] == s3
        # 旧船待确认回执已先行撤下，货代按提示重新申请即可
        assert quota_for_cargo(c1)["status"] == "已撤销"

        # 换到只有 80 吨载重的 S5：60 <= 80，允许
        ok_to_s5, msg = svc.run_action(v1, "调整船舶", str(s5))
        assert ok_to_s5 is not None, msg
        assert str(store.find("voyage", v1)["船舶编号"]) == str(s5)


class TestQuotaGuards:
    """回执签发与状态流转的守卫规则。"""

    def test_duplicate_active_quota_for_same_cargo_rejected(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "一票一回执", 20)
        first, _ = _quota(qsvc, s4, v1, c1, 20)
        assert first is not None
        second, msg = _quota(qsvc, s4, v1, c1, 20)
        assert second is None and "已持有有效回执" in msg

    def test_quota_vessel_must_match_voyage(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4, s3 = ctx["s4"], ctx["s3"]
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "凭证不符货", 10)
        quota, msg = _quota(qsvc, s3, v1, c1, 10)
        assert quota is None and "不一致" in msg

    def test_revoked_quota_cannot_be_consumed(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "撤单货", 10)
        quota, _ = _quota(qsvc, s4, v1, c1, 10)
        revoked, msg = qsvc.run_action(quota["id"], "撤下回执")
        assert revoked is not None
        # 已撤销回执不能再占用
        again, msg = qsvc.run_action(quota["id"], "确认占用")
        assert again is None and "待确认" in msg
        loaded, msg = csvc.run_action(c1, "确认装载")
        assert loaded is None

    def test_revoke_helper_only_targets_pending(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        c_pending = _cargo(csvc, v1, "待装载货", 10)
        c_loaded = _cargo(csvc, v1, "已装载货", 20)
        q_pending, _ = _quota(qsvc, s4, v1, c_pending, 10)
        q_loaded, _ = _quota(qsvc, s4, v1, c_loaded, 20)
        csvc.run_action(c_loaded, "确认装载")

        revoked = revoke_pending_quotas_for_voyage(v1)
        assert [q["id"] for q in revoked] == [q_pending["id"]]
        assert store.find("load_quota", q_pending["id"])["status"] == "已撤销"
        assert store.find("load_quota", q_loaded["id"])["status"] == "已占用"

    def test_readiness_distinguishes_waiting_and_ready(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        c_wait = _cargo(csvc, v1, "等待货", 10)
        c_ready = _cargo(csvc, v1, "可履约货", 10)
        _quota(qsvc, s4, v1, c_ready, 10)

        waiting = csvc.get_entry(c_wait)
        ready = csvc.get_entry(c_ready)
        assert waiting["配载状态"] == "等待配载"
        assert ready["配载状态"] == "已能履约"

    def test_adjust_quantity_requires_numeric_remark_and_rechecks_capacity(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        v2 = _voyage(svc, s4, "2026-12-05", "2026-12-15")
        c1 = _cargo(csvc, v1, "可调货60", 60)
        c2 = _cargo(csvc, v2, "相邻货30", 30)
        quota1, _ = _quota(qsvc, s4, v1, c1, 60)
        _quota(qsvc, s4, v2, c2, 30)
        csvc.run_action(c1, "确认装载")

        # 非数字备注被拒绝
        bad, msg = csvc.run_action(c1, "调整数量", "再多一点")
        assert bad is None and "总吨数" in msg

        # 60 → 80：与相邻航次 30 合计 110 > 100，拦截
        over, msg = csvc.run_action(c1, "调整数量", "80")
        assert over is None and "超过船舶载重吨" in msg

        # 60 → 70：70 + 30 = 100，刚好不超
        ok, msg = csvc.run_action(c1, "调整数量", "70")
        assert ok is not None, msg
        assert store.find("load_quota", quota1["id"])["预留吨数"] == 70.0

    def test_zero_or_negative_quota_rejected(self, s4_context):
        ctx = s4_context
        svc, qsvc, csvc = ctx["voyage_svc"], ctx["quota_svc"], ctx["cargo_svc"]
        s4 = ctx["s4"]
        v1 = _voyage(svc, s4, "2026-12-01", "2026-12-10")
        c1 = _cargo(csvc, v1, "零吨货", 0)
        quota, msg = _quota(qsvc, s4, v1, c1, 0)
        assert quota is None and "大于 0" in msg
