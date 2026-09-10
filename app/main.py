"""FastAPI 应用入口与路由。

接口一览
--------
POST   /api/plans                创建方案（规范化输入 → 幂等键，重复请求复用结果）
GET    /api/plans                列出已有方案
GET    /api/plans/{plan_id}      查询方案
POST   /api/plans/{plan_id}/resolve  重新求解（可用新 time_limit，旧结果版本化覆盖）
GET    /api/plans/{plan_id}/svg      SVG 预览
GET    /api/plans/{plan_id}/export   JSON 导出
GET    /healthz                   健康检查
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from . import db
from .schemas import PlanRequest, SemanticError, semantic_validate
from .service import get_or_create, plan_response, run_solve
from .svg import render_svg

app = FastAPI(
    title="矩形板材下料优化 API",
    description=(
        "面向小型木工坊的矩形板材贯穿式直切（guillotine）排样服务。\n\n"
        "- 提交板材规格/库存与零件长宽/数量、锯缝、边距、旋转与纹理设置；\n"
        "- 按「先少用板材、再降低废料率」生成**可复现**的排样结果；\n"
        "- 返回每块板上零件坐标、旋转状态、切割顺序、剩余区域与利用率，"
        "并列出无法安放的零件及原因；\n"
        "- 规范化输入生成幂等键，重复请求复用结果；支持求解时长上限，超时返回当前最佳；\n"
        "- 提供 SVG 预览与 JSON 导出；非法输入统一返回带字段路径的错误。"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------- #
# 统一错误格式
# --------------------------------------------------------------------------- #
def _error_body(code: str, message: str, errors: list[dict]) -> dict:
    return {"error": {"code": code, "message": message, "details": errors}}


@app.exception_handler(RequestValidationError)
async def _on_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"loc": list(e["loc"])[1:], "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body("validation_error", "请求参数校验失败", details),
    )


@app.exception_handler(SemanticError)
async def _on_semantic(_: Request, exc: SemanticError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_body("semantic_error", "输入存在相互矛盾或业务不合法的设置", exc.errors),
    )


@app.exception_handler(ValidationError)
async def _on_model_validation(_: Request, exc: ValidationError) -> JSONResponse:
    details = [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body("validation_error", "请求参数校验失败", details),
    )


def _parse_plan_request(raw: dict[str, Any]) -> PlanRequest:
    """字段级 + 语义级校验合并为一次 422 返回。"""
    req = PlanRequest.model_validate(raw)
    semantic_validate(req)
    return req


def _require_plan(conn, plan_id: str) -> dict:
    record = db.get_plan(conn, plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"方案 {plan_id!r} 不存在")
    return record


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.get("/healthz", tags=["系统"])
def healthz() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/plans",
    tags=["方案"],
    status_code=201,
    summary="创建方案（幂等：相同规范化输入复用已有结果）",
)
def create_plan(payload: dict) -> JSONResponse:
    req = _parse_plan_request(payload)
    with db.get_conn() as conn:
        body, reused = get_or_create(conn, req)
    return JSONResponse(status_code=200 if reused else 201, content=body)


@app.get("/api/plans", tags=["方案"], summary="列出全部方案")
def list_plans() -> dict:
    with db.get_conn() as conn:
        return {"plans": db.list_plan_ids(conn)}


@app.get("/api/plans/{plan_id}", tags=["方案"], summary="查询方案完整结果")
def get_plan(plan_id: str) -> dict:
    with db.get_conn() as conn:
        return plan_response(_require_plan(conn, plan_id))


@app.post(
    "/api/plans/{plan_id}/resolve",
    tags=["方案"],
    summary="重新求解（可调整 time_limit；结果版本递增覆盖）",
)
def resolve_plan(plan_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    with db.get_conn() as conn:
        record = _require_plan(conn, plan_id)
        # 以原始规范化请求为基础，允许覆盖 time_limit 与 name
        merged = dict(record["request"])
        if "time_limit" in payload:
            merged["time_limit"] = payload["time_limit"]
        else:
            merged["time_limit"] = 5.0
        if "name" in payload:
            merged["name"] = payload["name"]
        req = _parse_plan_request(merged)
        result = run_solve(req)
        db.update_result(conn, plan_id, json.dumps(result, ensure_ascii=False))
        conn.commit()
        record = db.get_plan(conn, plan_id)
        return plan_response(record, reused=False)


@app.get(
    "/api/plans/{plan_id}/svg",
    tags=["导出"],
    summary="SVG 排样预览",
    response_class=PlainTextResponse,
)
def plan_svg(plan_id: str) -> Response:
    with db.get_conn() as conn:
        record = _require_plan(conn, plan_id)
    svg = render_svg(record)
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/plans/{plan_id}/export", tags=["导出"], summary="JSON 导出")
def export_plan(plan_id: str) -> dict:
    with db.get_conn() as conn:
        record = _require_plan(conn, plan_id)
    return {
        "format": "nesting-plan-export/1.0",
        "plan_id": record["plan_id"],
        "idempotency_key": record["idempotency_key"],
        "name": record["name"],
        "version": record["version"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "request": record["request"],
        "solution": record["result"],
    }
