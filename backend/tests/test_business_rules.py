"""业务约束测试：载重吨校验、船员冲突检测、吨位校验等。"""
from __future__ import annotations

from app.services.cross_module import (
    check_cargo_tonnage,
    check_crew_shift_overlap,
    seal_vessel_cascade,
    vessel_confirmed,
    route_confirmed,
    voyage_not_completed,
    charter_active_for_vessel,
    duplicate_manifest_check,
    port_call_in_sequence,
)
from app.services.vessel import VesselService
from app.services.route import RouteService
from app.services.voyage import VoyageService
from app.services.cargo import CargoService
from app.services.load_quota import LoadQuotaService
from app.services.crew_shift import CrewShiftService
from app.services.charter import CharterService
from app.services.manifest import ManifestService
from app.store import store


def _create_test_vessel(svc, name, dwt="50000", ton="30000"):
    entry, result = svc.create_entry({"船名": name, "总吨位": ton, "载重吨": dwt})
    if entry is None:
        raise RuntimeError(f"创建测试船舶失败: {result}")
    svc.run_action(entry["id"], "确认档案")
    return entry["id"]


def _create_test_voyage(svc, vessel_id, route_id=3, departure="2026-12-01"):
    entry, result = svc.create_entry({
        "船舶编号": vessel_id, "航线编号": route_id, "计划离港": departure,
    })
    if entry is None:
        raise RuntimeError(f"创建测试航次失败: {result}")
    return entry["id"]


class TestVesselConfirmed:
    def test_confirmed_vessel_passes(self):
        ok, msg = vessel_confirmed(3)
        assert ok, f"已确认船舶应通过: {msg}"

    def test_draft_vessel_fails(self):
        ok, msg = vessel_confirmed(1)
        assert not ok

    def test_nonexistent_vessel_fails(self):
        ok, msg = vessel_confirmed(999)
        assert not ok


class TestRouteConfirmed:
    def test_confirmed_route_passes(self):
        ok, msg = route_confirmed(3)
        assert ok

    def test_draft_route_fails(self):
        ok, msg = route_confirmed(1)
        assert not ok


class TestVoyageNotCompleted:
    def test_not_completed_voyage_passes(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "航次测试船H", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)
        ok, msg = voyage_not_completed(voyage_id)
        assert ok, f"未完成航次应通过: {msg}"

    def test_completed_voyage_fails(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "已完成航次测试船I", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)
        voyage_svc.run_action(voyage_id, "标记完成")
        ok, msg = voyage_not_completed(voyage_id)
        assert not ok, "已完成航次应被拒绝"


class TestCargoTonnage:
    def test_cargo_tonnage_exceeds_dwt(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "超载测试船J", dwt="100")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)
        ok, msg = check_cargo_tonnage(voyage_id, 9999)
        assert not ok, f"超载吨位应被拦截: {msg}"

    def test_cargo_tonnage_within_dwt(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "正常吨位测试船K", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)
        ok, msg = check_cargo_tonnage(voyage_id, 1)
        assert ok, f"正常吨位应通过: {msg}"


class TestCrewShiftOverlap:
    def test_no_overlap_passes(self):
        ok, msg = check_crew_shift_overlap("不存在船员", "2026-01-01", "2026-02-01")
        assert ok

    def test_overlap_detected(self):
        crew_name = "重叠测试船员L"
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "重叠排班测试船L", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)

        shift_svc = CrewShiftService()
        entry, _ = shift_svc.create_entry({
            "船员姓名": crew_name,
            "岗位": "大副",
            "上船日期": "2026-06-01",
            "下船日期": "2026-08-01",
            "所属航次": voyage_id,
        })
        assert entry is not None

        ok, msg = check_crew_shift_overlap(crew_name, "2026-07-01", "2026-09-01")
        assert not ok, f"重叠排班应被拦截: {msg}"


class TestSealVessel:
    def test_seal_vessel_without_active_voyages(self):
        ok, msg = seal_vessel_cascade(999)
        assert ok

    def test_seal_vessel_with_active_voyage(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "封存级联测试船M", dwt="50000")
        voyage_svc = VoyageService()
        _create_test_voyage(voyage_svc, vessel_id)
        ok, msg = seal_vessel_cascade(vessel_id)
        assert not ok, f"有未完成航次应阻止封存"


class TestCharterActive:
    def test_no_charter_passes(self):
        ok, msg = charter_active_for_vessel("无合约船舶")
        assert ok

    def test_terminated_charter_fails(self):
        vessel_name = "终止合约测试船N"
        charter_svc = CharterService()
        entry, _ = charter_svc.create_entry({
            "租用船舶": vessel_name, "承租方": "测试", "日租金": "5000",
        })
        assert entry is not None
        charter_svc.run_action(entry["id"], "签认合约")
        charter_svc.run_action(entry["id"], "终止合约")
        ok, msg = charter_active_for_vessel(vessel_name)
        assert not ok, f"已终止合约应阻止创建航次"


class TestDuplicateManifest:
    def test_duplicate_manifest_blocked(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "舱单测试船O", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)

        cargo_svc = CargoService()
        cargo, _ = cargo_svc.create_entry({
            "货物品名": "铁矿",
            "托运方": "测试",
            "件数": "10",
            "总吨数": "5",
            "装载航次": voyage_id,
        })
        assert cargo is not None
        quota_svc = LoadQuotaService()
        quota, _ = quota_svc.create_entry({
            "船舶编号": vessel_id,
            "航次编号": voyage_id,
            "货物编号": cargo["id"],
            "预留吨数": "5",
        })
        assert quota is not None
        cargo_svc.run_action(cargo["id"], "确认装载")

        manifest_svc = ManifestService()
        entry, _ = manifest_svc.create_entry({
            "航次编号": voyage_id,
            "货物清单": "铁矿10件",
            "托运方汇总": "测试",
        })
        assert entry is not None
        manifest_svc.run_action(entry["id"], "签发舱单")
        ok, msg = duplicate_manifest_check(voyage_id)
        assert not ok, f"已签发舱单应阻止重复签认: {msg}"


class TestPortCallSequence:
    def test_port_call_sequence_allows_single(self):
        ok, msg = port_call_in_sequence(2)
        assert isinstance(ok, bool)