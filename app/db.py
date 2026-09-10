"""SQLite 持久化与幂等键。

幂等键 = 对「除 name / time_limit 之外的规范化请求」做 SHA-256：
相同规格、数量、锯缝、边距、旋转与纹理设置的请求永远得到同一个键，
重复提交直接复用已存方案；time_limit 不参与键，允许重算时调整预算。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "plans.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id        TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    name           TEXT,
    request_json   TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    reused_count   INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


def canonical_request(req: dict[str, Any]) -> dict[str, Any]:
    """规范化请求：剔除 name / time_limit，列表按 id 排序。"""
    boards = sorted(
        (dict(b) for b in req["boards"]),
        key=lambda b: b["id"],
    )
    parts = sorted(
        (dict(p) for p in req["parts"]),
        key=lambda p: p["id"],
    )
    return {
        "kerf": round(float(req["kerf"]), 6),
        "boards": [
            {
                "id": b["id"],
                "length": round(float(b["length"]), 6),
                "width": round(float(b["width"]), 6),
                "quantity": int(b["quantity"]),
                "margin": round(float(b.get("margin", 0.0)), 6),
                "grain": b.get("grain", "none"),
            }
            for b in boards
        ],
        "parts": [
            {
                "id": p["id"],
                "length": round(float(p["length"]), 6),
                "width": round(float(p["width"]), 6),
                "quantity": int(p["quantity"]),
                "allow_rotation": bool(p.get("allow_rotation", True)),
                "grain": p.get("grain", "none"),
                "margin": (
                    round(float(p["margin"]), 6) if p.get("margin") is not None else None
                ),
            }
            for p in parts
        ],
    }


def make_idempotency_key(req: dict[str, Any]) -> str:
    blob = json.dumps(canonical_request(req), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get_plan(conn: sqlite3.Connection, plan_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    return {
        "plan_id": row["plan_id"],
        "idempotency_key": row["idempotency_key"],
        "name": row["name"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
        "version": row["version"],
        "reused_count": row["reused_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_plan_by_key(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM plans WHERE idempotency_key = ?", (key,)).fetchone()
    if row is None:
        return None
    return {
        "plan_id": row["plan_id"],
        "idempotency_key": row["idempotency_key"],
        "name": row["name"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
        "version": row["version"],
        "reused_count": row["reused_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    key: str,
    name: Optional[str],
    request_json: str,
    result_json: str,
) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO plans (plan_id, idempotency_key, name, request_json, result_json,
                           version, reused_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)
        """,
        (plan_id, key, name, request_json, result_json, now, now),
    )


def touch_reused(conn: sqlite3.Connection, plan_id: str) -> None:
    conn.execute(
        "UPDATE plans SET reused_count = reused_count + 1, updated_at = ? WHERE plan_id = ?",
        (time.time(), plan_id),
    )


def update_result(conn: sqlite3.Connection, plan_id: str, result_json: str) -> None:
    conn.execute(
        "UPDATE plans SET result_json = ?, version = version + 1, updated_at = ? WHERE plan_id = ?",
        (result_json, time.time(), plan_id),
    )


def list_plan_ids(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT plan_id, name, version, reused_count, created_at, updated_at "
        "FROM plans ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
