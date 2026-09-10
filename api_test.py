#!/usr/bin/env python3
"""端到端接口测试（需要先在 127.0.0.1:8000 启动服务）。"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
PASS = 0


def call(method, path, body=None, expect=None, raw=False):
    global PASS
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            code, payload = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read().decode()
    parsed = payload if raw else json.loads(payload)
    if expect is not None:
        assert code == expect, f"{method} {path} -> {code} (期望 {expect}): {payload[:300]}"
        PASS += 1
    return code, parsed


def main():
    # 健康检查 & OpenAPI
    code, h = call("GET", "/healthz", expect=200)
    assert h["status"] == "ok"
    code, spec = call("GET", "/openapi.json", expect=200)
    assert "/api/plans" in spec["paths"]
    print("1. 健康检查 / OpenAPI OK")

    # 非法尺寸
    code, e = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 2,
        "boards": [{"id": "b1", "length": 0, "width": 1220, "quantity": 2}],
        "parts": [{"id": "p1", "length": 100, "width": 100, "quantity": 1}],
    }, expect=422)
    locs = [tuple(d["loc"]) for d in e["error"]["details"]]
    assert ("boards", 0, "length") in locs, locs
    print("2. 非法尺寸 ->", locs)

    # 非正数量
    code, e = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 2,
        "boards": [{"id": "b1", "length": 1000, "width": 1000, "quantity": 1}],
        "parts": [{"id": "p1", "length": 100, "width": 100, "quantity": 0}],
    }, expect=422)
    assert any(d["loc"] == ["parts", 0, "quantity"] for d in e["error"]["details"])
    print("3. 非正数量 OK")

    # 重复标识
    code, e = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 2,
        "boards": [{"id": "x", "length": 1000, "width": 1000, "quantity": 1}],
        "parts": [
            {"id": "x", "length": 100, "width": 100, "quantity": 1},
            {"id": "x", "length": 100, "width": 100, "quantity": 1},
        ],
    }, expect=422)
    types = [d["type"] for d in e["error"]["details"]]
    assert "duplicate_id" in types, e
    print("4. 重复标识 OK:", [d["msg"] for d in e["error"]["details"]])

    # 旋转/纹理矛盾
    code, e = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 2,
        "boards": [{"id": "oak", "length": 2000, "width": 800, "quantity": 1,
                    "grain": "along_length"}],
        "parts": [{"id": "rail", "length": 1200, "width": 120, "quantity": 2,
                   "allow_rotation": False, "grain": "across_length"}],
    }, expect=422)
    assert e["error"]["code"] == "semantic_error"
    assert any(d["type"] == "grain_rotation_conflict" for d in e["error"]["details"])
    print("5. 旋转/纹理矛盾 OK:", e["error"]["details"][0]["msg"])

    # 边距过大
    code, e = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 2,
        "boards": [{"id": "b", "length": 200, "width": 200, "quantity": 1, "margin": 110}],
        "parts": [{"id": "p", "length": 50, "width": 50, "quantity": 1}],
    }, expect=422)
    assert any(d["type"] == "margin_too_large" for d in e["error"]["details"])
    print("6. 边距过大 OK")

    # 正常创建
    payload = {
        "name": "衣柜门板",
        "kerf": 3.0, "time_limit": 2.0,
        "boards": [{"id": "plywood", "length": 2440, "width": 1220, "quantity": 5, "margin": 10}],
        "parts": [
            {"id": "door", "length": 600, "width": 400, "quantity": 6},
            {"id": "shelf", "length": 800, "width": 300, "quantity": 4},
            {"id": "panel", "length": 1000, "width": 400, "quantity": 2,
             "allow_rotation": False, "grain": "along_length"},
        ],
    }
    code, r = call("POST", "/api/plans", payload, expect=201)
    pid = r["plan_id"]
    assert r["reused"] is False
    sol = r["solution"]
    assert sol["summary"]["placed_parts"] == 12, sol["summary"]
    assert sol["summary"]["unplaced_parts"] == 0
    assert len(sol["boards"]) >= 1
    b0 = sol["boards"][0]
    assert b0["cuts"], "应有切割顺序"
    assert all("seq" in c and "position" in c for c in b0["cuts"])
    assert all(p["grain_axis"] in ("x", "y", "none") for p in b0["parts"])
    print(f"7. 创建方案 OK: {pid} 用板 {sol['summary']['boards_used']} 张, "
          f"利用率 {sol['summary']['utilization_rate']:.1%}, 刀数 {len(b0['cuts'])}")

    # 幂等：重复提交
    code, r2 = call("POST", "/api/plans", payload, expect=200)
    assert r2["plan_id"] == pid and r2["reused"] is True and r2["reused_count"] == 1
    # 改变 name 不影响幂等键；改变 time_limit 也不影响
    p3 = dict(payload, name="另一个名字", time_limit=0.1)
    code, r3 = call("POST", "/api/plans", p3, expect=200)
    assert r3["plan_id"] == pid
    print("8. 幂等复用 OK（name/time_limit 不参与键）")

    # 查询
    code, g = call("GET", f"/api/plans/{pid}", expect=200)
    assert g["solution"] == sol
    code, lst = call("GET", "/api/plans", expect=200)
    assert any(p["plan_id"] == pid for p in lst["plans"])
    print("9. 查询/列表 OK")

    # 404
    code, e = call("GET", "/api/plans/deadbeefdeadbeef", expect=404)
    print("10. 不存在方案 404 OK")

    # 重新求解（更短预算）
    code, rr = call("POST", f"/api/plans/{pid}/resolve", {"time_limit": 0.05}, expect=200)
    assert rr["version"] == g["version"] + 1, rr["version"]
    print(f"11. 重新求解 OK, version -> {rr['version']}, "
          f"status={rr['solution']['status']}, 用板 {rr['solution']['summary']['boards_used']}")

    # SVG
    code, svg = call("GET", f"/api/plans/{pid}/svg", expect=200, raw=True)
    assert svg.lstrip().startswith("<svg") and "<text" in svg
    print(f"12. SVG OK ({len(svg)} 字节)")

    # JSON 导出
    code, exp = call("GET", f"/api/plans/{pid}/export", expect=200)
    assert exp["format"].startswith("nesting-plan-export")
    assert exp["solution"]["boards"][0]["parts"]
    print("13. JSON 导出 OK")

    # 无法安放的零件及原因
    code, u = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 1,
        "boards": [{"id": "tiny", "length": 300, "width": 300, "quantity": 1}],
        "parts": [
            {"id": "big", "length": 500, "width": 100, "quantity": 1},
            {"id": "ok", "length": 100, "width": 100, "quantity": 1},
        ],
    }, expect=201)
    us = u["solution"]
    assert us["summary"]["placed_parts"] == 1
    assert us["unplaced"][0]["part_id"] == "big"
    assert us["unplaced"][0]["reason"] == "too_large_for_any_board"
    print("14. 无法安放零件原因 OK:", us["unplaced"][0]["detail"])

    # 库存耗尽
    code, u2 = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 1,
        "boards": [{"id": "b", "length": 500, "width": 500, "quantity": 1}],
        "parts": [{"id": "a", "length": 400, "width": 400, "quantity": 3}],
    }, expect=201)
    reasons = {x["reason"] for x in u2["solution"]["unplaced"]}
    assert reasons == {"no_board_inventory_left"}, reasons
    print("15. 库存耗尽原因 OK")

    # time_limit 非法
    code, e = call("POST", "/api/plans", {
        "kerf": 3, "time_limit": 0,
        "boards": [{"id": "b", "length": 500, "width": 500, "quantity": 1}],
        "parts": [{"id": "a", "length": 100, "width": 100, "quantity": 1}],
    }, expect=422)
    print("16. time_limit 非法 OK")

    print(f"\n全部端到端断言通过 ✅（{PASS} 个状态码断言）")


if __name__ == "__main__":
    main()
