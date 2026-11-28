"""跨模块链路集成测试：覆盖航次编排协作台的至少 5 条真实数据流。"""
from __future__ import annotations

from app.services.vessel import VesselService
from app.services.route import RouteService
from app.services.voyage import VoyageService
from app.services.cargo import CargoService
from app.services.load_quota import LoadQuotaService
from app.services.crew_shift import CrewShiftService
from app.services.manifest import ManifestService
from app.services.charter import CharterService
from app.services.clearance import ClearanceService
from app.store import store


def _create_test_vessel(svc, name, dwt="50000", ton="30000"):
    """创建一条可用于跨模块测试的船舶。"""
    entry, result = svc.create_entry({
        "船名": name,
        "总吨位": ton,
        "载重吨": dwt,
    })
    if entry is None:
        raise RuntimeError(f"创建测试船舶失败: {result}")
    svc.run_action(entry["id"], "确认档案")
    return entry["id"]


def _create_test_voyage(svc, vessel_id, route_id=3, departure="2026-12-01", arrival="2026-12-10"):
    entry, result = svc.create_entry({
        "船舶编号": vessel_id,
        "航线编号": route_id,
        "计划离港": departure,
        "计划到港": arrival,
    })
    if entry is None:
        raise RuntimeError(f"创建测试航次失败: {result}")
    return entry["id"]


def _create_test_cargo(svc, voyage_id, name="测试货", tons="10"):
    entry, result = svc.create_entry({
        "货物品名": name,
        "托运方": "测试公司",
        "件数": "100",
        "总吨数": tons,
        "装载航次": voyage_id,
    })
    if entry is None:
        raise RuntimeError(f"创建测试货物失败: {result}")
    return entry["id"]


def _issue_quota(svc, vessel_id, voyage_id, cargo_id, tons):
    entry, result = svc.create_entry({
        "船舶编号": vessel_id,
        "航次编号": voyage_id,
        "货物编号": cargo_id,
        "预留吨数": str(tons),
        "申请货代": "测试货代",
    })
    if entry is None:
        raise RuntimeError(f"签发装载额度回执失败: {result}")
    return entry["id"]


class TestVesselRouteToVoyage:
    """链路1：船舶+航线→航次"""

    def test_create_voyage_with_confirmed_vessel_and_route(self):
        svc = VoyageService()
        entry, result = svc.create_entry({
            "船舶编号": 3,
            "航线编号": 3,
            "计划离港": "2026-12-01",
        })
        assert entry is not None, f"创建航次应成功: {result}"

    def test_create_voyage_with_draft_vessel_fails(self):
        svc = VoyageService()
        entry, result = svc.create_entry({
            "船舶编号": 1,
            "航线编号": 3,
            "计划离港": "2026-12-01",
        })
        assert entry is None, "未确认船舶应阻止创建航次"

    def test_create_voyage_with_nonexistent_route_fails(self):
        svc = VoyageService()
        entry, result = svc.create_entry({
            "船舶编号": 3,
            "航线编号": 999,
            "计划离港": "2026-12-01",
        })
        assert entry is None


class TestVoyageToCargo:
    """链路2：航次→货物→装载额度回执"""

    def test_cargo_tonnage_exceeds_dwt(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "超载测试船", dwt="100")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)

        cargo_svc = CargoService()
        cargo_id = _create_test_cargo(cargo_svc, voyage_id, tons="999")

        # 货物登记允许先挂账等待配载，但申请额度回执时必须按载重阈值拦截
        quota_svc = LoadQuotaService()
        quota, result = quota_svc.create_entry({
            "船舶编号": vessel_id,
            "航次编号": voyage_id,
            "货物编号": cargo_id,
            "预留吨数": "999",
        })
        assert quota is None, f"超载吨位应在额度回执环节拦截: {result}"

        # 没有有效回执时确认装载同样被拒绝
        loaded, msg = cargo_svc.run_action(cargo_id, "确认装载")
        assert loaded is None and "装载额度回执" in msg

    def test_cargo_tonnage_within_dwt(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "正常载重测试船A", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)

        cargo_svc = CargoService()
        entry, result = cargo_svc.create_entry({
            "货物品名": "谷物",
            "托运方": "测试公司",
            "件数": "100",
            "总吨数": "30",
            "装载航次": voyage_id,
        })
        assert entry is not None, f"正常吨位应通过: {result}"

    def test_cargo_with_completed_voyage_fails(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "已完成航次货物测试船B", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)
        voyage_svc.run_action(voyage_id, "标记完成")

        cargo_svc = CargoService()
        entry, result = cargo_svc.create_entry({
            "货物品名": "铁矿",
            "托运方": "测试",
            "件数": "10",
            "装载航次": voyage_id,
        })
        assert entry is None, "已完成航次应拦截货物登记"


class TestVoyageToCrewShift:
    """链路3：航次→船员"""

    def test_crew_shift_overlap_detection(self):
        crew_name = "重叠检测船员C"
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "船员重叠测试船C", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)

        shift_svc = CrewShiftService()
        entry, _ = shift_svc.create_entry({
            "船员姓名": crew_name,
            "岗位": "大副",
            "上船日期": "2026-10-01",
            "下船日期": "2026-12-01",
            "所属航次": voyage_id,
        })
        assert entry is not None

        entry2, result2 = shift_svc.create_entry({
            "船员姓名": crew_name,
            "岗位": "船长",
            "上船日期": "2026-11-15",
            "下船日期": "2027-01-01",
            "所属航次": voyage_id,
        })
        assert entry2 is None, f"重叠排班应被拦截: {result2}"


class TestPortCallToClearance:
    """链路4：挂靠→通关"""

    def test_clearance_with_voyage(self):
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, 3)
        svc = ClearanceService()
        entry, result = svc.create_entry({
            "航次编号": voyage_id,
            "申报类型": "进出口",
            "申报港口": "上海港",
        })
        assert entry is not None, f"通关申报应成功: {result}"


class TestCargoToManifest:
    """链路5：货物→舱单"""

    def test_manifest_without_loaded_cargo_fails(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "无货舱单测试船D", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)

        manifest_svc = ManifestService()
        manifest, _ = manifest_svc.create_entry({
            "航次编号": voyage_id,
            "货物清单": "测试",
            "托运方汇总": "测试",
        })
        assert manifest is not None
        result, msg = manifest_svc.run_action(manifest["id"], "签发舱单")
        assert result is None or "没有已装载" in str(msg)

    def test_manifest_with_loaded_cargo_succeeds(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "有货舱单测试船E", dwt="50000")
        voyage_svc = VoyageService()
        voyage_id = _create_test_voyage(voyage_svc, vessel_id)

        cargo_svc = CargoService()
        cargo_id = _create_test_cargo(cargo_svc, voyage_id, name="煤炭", tons="3")
        quota_svc = LoadQuotaService()
        quota_id = _issue_quota(quota_svc, vessel_id, voyage_id, cargo_id, 3)
        result, msg = cargo_svc.run_action(cargo_id, "确认装载")
        assert result is not None, f"凭有效回执确认装载应成功: {msg}"
        assert result["额度回执"] == quota_id

        manifest_svc = ManifestService()
        manifest, _ = manifest_svc.create_entry({
            "航次编号": voyage_id,
            "货物清单": "煤炭50件",
            "托运方汇总": "测试",
        })
        assert manifest is not None
        result, msg = manifest_svc.run_action(manifest["id"], "签发舱单")
        assert result is not None, f"签发舱单应成功: {msg}"

        # 重复签认幂等
        result2, msg2 = manifest_svc.run_action(manifest["id"], "签发舱单")
        assert result2 is not None or "已签发" in str(msg2)


class TestCharterToVoyage:
    """链路6：租船合约→航次"""

    def test_charter_terminated_blocks_voyage(self):
        vessel_name = "合约终止测试船F"
        charter_svc = CharterService()
        charter, _ = charter_svc.create_entry({
            "租用船舶": vessel_name,
            "承租方": "测试",
            "日租金": "5000",
        })
        assert charter is not None
        charter_svc.run_action(charter["id"], "签认合约")
        charter_svc.run_action(charter["id"], "终止合约")

        svc = VoyageService()
        entry, result = svc.create_entry({
            "船舶编号": vessel_name,
            "航线编号": 3,
            "计划离港": "2026-12-01",
        })
        assert entry is None, "已终止合约应阻止创建航次"


class TestSealVesselCascade:
    """链路8：船舶封存→级联"""

    def test_seal_vessel_with_active_voyage_fails(self):
        vessel_svc = VesselService()
        vessel_id = _create_test_vessel(vessel_svc, "封存测试船G", dwt="50000")
        voyage_svc = VoyageService()
        _create_test_voyage(voyage_svc, vessel_id)

        entry, msg = vessel_svc.run_action(vessel_id, "封存船舶")
        assert entry is None, f"有未完成航次应阻止封存: {msg}"