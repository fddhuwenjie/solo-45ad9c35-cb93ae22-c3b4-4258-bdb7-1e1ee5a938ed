"""服务层：请求模型 ↔ 求解器实体映射与求解封装。"""
from __future__ import annotations

import json

from . import db
from .schemas import PlanRequest
from .solver import BoardType, PartType, solve as solver_solve


def _to_entities(req: PlanRequest) -> tuple[list[BoardType], list[PartType], float, float]:
    boards = [
        BoardType(
            id=b.id,
            length=float(b.length),
            width=float(b.width),
            quantity=int(b.quantity),
            margin=float(b.margin),
            grain=b.grain.value,
        )
        for b in sorted(req.boards, key=lambda b: b.id)
    ]
    parts = [
        PartType(
            id=p.id,
            length=float(p.length),
            width=float(p.width),
            quantity=int(p.quantity),
            allow_rotation=bool(p.allow_rotation),
            grain=p.grain.value,
            margin=None if p.margin is None else float(p.margin),
        )
        for p in sorted(req.parts, key=lambda p: p.id)
    ]
    return boards, parts, float(req.kerf), float(req.time_limit)


def run_solve(req: PlanRequest) -> dict:
    boards, parts, kerf, time_limit = _to_entities(req)
    return solver_solve(boards, parts, kerf, time_limit)


def plan_response(record: dict, reused: bool | None = None) -> dict:
    return {
        "plan_id": record["plan_id"],
        "idempotency_key": record["idempotency_key"],
        "name": record["name"],
        "version": record["version"],
        "reused": reused,
        "reused_count": record["reused_count"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "request": record["request"],
        "solution": record["result"],
    }


def get_or_create(conn, req: PlanRequest) -> tuple[dict, bool]:
    """按幂等键取已有方案；不存在则求解并落库。返回 (响应体, 是否复用)。"""
    payload = req.model_dump(mode="json")
    key = db.make_idempotency_key(payload)
    existing = db.get_plan_by_key(conn, key)
    if existing is not None:
        db.touch_reused(conn, existing["plan_id"])
        conn.commit()
        existing["reused_count"] += 1
        return plan_response(existing, reused=True), True

    result = run_solve(req)
    plan_id = key[:16]
    db.save_plan(
        conn,
        plan_id=plan_id,
        key=key,
        name=req.name,
        request_json=json.dumps(db.canonical_request(payload), ensure_ascii=False),
        result_json=json.dumps(result, ensure_ascii=False),
    )
    conn.commit()
    record = db.get_plan(conn, plan_id)
    return plan_response(record, reused=False), False
