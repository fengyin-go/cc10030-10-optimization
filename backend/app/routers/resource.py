"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, result = service.create_entry(payload.values)
    if entry is None:
        # 领域服务可能返回缺失字段列表，也可能直接返回跨模块校验失败原因
        if isinstance(result, list):
            return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(result)}")
        return ActionResult(ok=False, message=str(result))
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip(), payload.remark)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.get("/cargo/{entry_id}/readiness", response_model=dict)
def cargo_readiness(entry_id: int) -> dict[str, Any]:
    """货代查询某票货物的配载状态：还在等待、已能履约，以及何时能继续。"""
    from app.services.cross_module import cargo_loading_readiness, quota_availability
    from app.store import store as data_store

    cargo = data_store.find("cargo", entry_id)
    if cargo is None:
        raise HTTPException(status_code=404, detail=f"货物 {entry_id} 不存在或已归档")

    readiness = cargo_loading_readiness(entry_id)
    voyage_id = cargo.get("装载航次", "")
    availability: dict[str, Any] = {}
    voyage = data_store.find("voyage", voyage_id) if str(voyage_id).isdigit() else None
    if voyage is not None:
        from app.services.cross_module import voyage_window
        availability = quota_availability(
            voyage.get("船舶编号", ""), voyage_window(voyage),
        )
    return {"cargo_id": entry_id, **readiness, "vessel_quota": availability}
