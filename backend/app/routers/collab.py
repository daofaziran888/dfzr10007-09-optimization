"""看板协作管理接口：维护协作看板，覆盖发布看板、归档看板、删除看板等动作。

版本化数据流：
- POST /api/collab/{id}/refresh-frequency  每次刷新频率变更生成新版本，成员范围与发布结果同版本切换；
- POST /api/collab/{id}/refresh-result     刷新发布结果，失败不覆盖上一份完整结果；
- GET  /api/collab/{id}/versions           版本追溯，旧版本按当时口径保留；
- POST /api/collab/{id}/share-links        签发共享链接，成员收紧后旧链接受控失效；
- GET  /api/collab/shared/{token}          共享链接只读视图；
- GET  /api/collab/{id}/export             导出入口，按操作人做权限与归属校验。

注意：字面量路径（/export、/shared/{token}）必须声明在 /{entry_id} 之前，
否则会被路径参数截获。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.collab import CollabService

router = APIRouter(prefix="/api/collab", tags=["看板协作管理"])

service = CollabService()

LIST_FIELDS = ["看板编号", "看板名称", "负责人", "协作成员", "图表集合", "刷新频率", "评论开关", "当前版本", "创建时间", "看板状态"]
STATUSES = ["草稿", "协作中", "已发布", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按看板编号检索"),
    status: str | None = Query(default=None, description="草稿、协作中、已发布、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按看板编号与状态过滤看板协作管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出看板协作管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "collab", "total": total, "items": items}


@router.get("/shared/{token}")
def get_shared(token: str) -> dict[str, Any]:
    """共享链接只读视图；链接失效时返回 410 与受控失效原因。"""
    payload, message, status_code = service.get_shared(token)
    if payload is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "data": payload}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条协作看板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"协作看板 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions")
def list_versions(
    entry_id: int,
    operator: str = Query(default="", description="操作人：负责人、管理员或协作成员"),
) -> dict[str, Any]:
    """版本追溯：返回该看板全部版本（含已归档），均按当时口径保留。"""
    versions, message, status_code = service.list_versions(entry_id, operator=operator)
    if versions is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "items": versions, "total": len(versions)}


@router.get("/{entry_id}/export")
def export_entry(
    entry_id: int,
    operator: str = Query(default="", description="操作人：决定导出权限与可见口径"),
) -> dict[str, Any]:
    """导出入口：对协作看板执行权限与归属校验；访客只能拿到只读版本。"""
    payload, message, status_code = service.export_entry(entry_id, operator=operator)
    if payload is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "data": payload}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条协作看板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="协作看板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条协作看板执行发布看板、归档看板、删除看板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/refresh-frequency")
def adjust_refresh_frequency(entry_id: int, payload: EntryPayload) -> dict[str, Any]:
    """调整刷新频率：生成新版本；同一变更重复提交幂等，并发冲突时回读已生效版本。"""
    values = payload.values
    result, message, status_code = service.adjust_refresh_frequency(
        entry_id,
        operator=str(values.get("operator") or ""),
        frequency=str(values.get("刷新频率") or ""),
        members=values.get("协作成员"),
        base_version=values.get("base_version"),
        note=str(values.get("变更说明") or ""),
    )
    if result is None:
        raise HTTPException(status_code=status_code, detail=message)
    if status_code == 409:
        raise HTTPException(status_code=409, detail={"message": message, "effective": result})
    return {"ok": True, "message": message, "data": result}


@router.post("/{entry_id}/refresh-result")
def refresh_publish_result(entry_id: int, payload: EntryPayload) -> dict[str, Any]:
    """刷新当前版本的发布结果；刷新失败只记录原因，不覆盖上一份完整发布结果。"""
    values = payload.values
    result, message, status_code = service.refresh_publish_result(
        entry_id,
        operator=str(values.get("operator") or ""),
        succeed=bool(values.get("成功", True)),
        reason=str(values.get("原因") or ""),
    )
    if result is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "data": result}


@router.post("/{entry_id}/share-links")
def create_share_link(entry_id: int, payload: EntryPayload) -> dict[str, Any]:
    """为当前生效版本签发共享链接；成员范围收紧后旧链接会受控失效。"""
    result, message, status_code = service.create_share_link(
        entry_id,
        operator=str(payload.values.get("operator") or ""),
    )
    if result is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "data": result}
