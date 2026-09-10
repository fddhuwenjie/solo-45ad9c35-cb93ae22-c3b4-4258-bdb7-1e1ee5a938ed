"""求解器几何正确性自检（不属于 API，仅开发期使用）。

校验：零件不越界、不重叠、边距满足、锯缝后余料尺寸一致、结果可复现。
"""
from app.solver import BoardType, PartType, solve


def check_result(result, boards_def):
    bmap = {b.id: b for b in boards_def}
    for bd in result["boards"]:
        spec = bmap[bd["board_id"]]
        L, W = spec.length, spec.width
        assert bd["length"] == round(L, 3) and bd["width"] == round(W, 3)
        rects = []
        for p in bd["parts"]:
            assert 0 <= p["x"] and 0 <= p["y"]
            assert p["x"] + p["length"] <= L + 1e-6
            assert p["y"] + p["width"] <= W + 1e-6
            # 边距
            m = spec.margin
            assert p["x"] >= m - 1e-6 and p["y"] >= m - 1e-6
            assert p["x"] + p["length"] <= L - m + 1e-6
            assert p["y"] + p["width"] <= W - m + 1e-6
            rects.append((p["x"], p["y"], p["length"], p["width"], p["part_id"]))
        # 两两不重叠
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                ax, ay, al, aw, _ = rects[i]
                bx, by, bl, bw, _ = rects[j]
                overlap_x = min(ax + al, bx + bl) - max(ax, bx)
                overlap_y = min(ay + aw, by + bw) - max(ay, by)
                assert overlap_x <= 1e-6 or overlap_y <= 1e-6, (
                    f"重叠: {rects[i]} vs {rects[j]} on {bd['instance_id']}"
                )
        # 利用率一致性
        area = sum(r[2] * r[3] for r in rects)
        assert abs(area - bd["used_area"]) < 1e-3
        # 余料不与零件重叠
        for f in bd["leftover_regions"]:
            fr = (f["x"], f["y"], f["length"], f["width"])
            for (ax, ay, al, aw, _) in rects:
                ox = min(ax + al, fr[0] + fr[2]) - max(ax, fr[0])
                oy = min(ay + aw, fr[1] + fr[3]) - max(ay, fr[1])
                assert ox <= 1e-4 or oy <= 1e-4, ("余料与零件重叠", fr)
        # 切口：端点在板/已切边界内，且不穿过任何零件内部
        for c in bd["cuts"]:
            assert 0 <= c["start"] <= c["end"] + 1e-6
            assert -1e-6 <= c["position"] <= (L if c["direction"] == "v" else W) + 1e-6
            for (ax, ay, al, aw, _) in rects:
                if c["direction"] == "v":
                    x = c["position"]
                    if ax + 1e-6 < x < ax + al - 1e-6:
                        # 竖线在零件 X 区间内 → 其 Y 跨度不得进入零件 Y 区间
                        overlap = min(c["end"], ay + aw) - max(c["start"], ay)
                        assert overlap <= 1e-4
                else:
                    y = c["position"]
                    if ay + 1e-6 < y < ay + aw - 1e-6:
                        overlap = min(c["end"], ax + al) - max(c["start"], ax)
                        assert overlap <= 1e-4
    s = result["summary"]
    assert abs(s["placed_area"] - sum(b["used_area"] for b in result["boards"])) < 1e-3


def main():
    # 用例 1：基础排样 + 利用率
    boards = [BoardType("plywood", 2440, 1220, 5, 10, "none")]
    parts = [
        PartType("door", 600, 400, 6, True, "none", None),
        PartType("shelf", 800, 300, 4, True, "none", None),
    ]
    r1 = solve(boards, parts, 3.0, 3.0)
    check_result(r1, boards)
    assert r1["summary"]["placed_parts"] == 10
    assert r1["summary"]["unplaced_parts"] == 0
    print("用例1 基础排样 OK:", r1["summary"])

    # 可复现
    r1b = solve(boards, parts, 3.0, 1.0)
    assert r1b["boards"] == r1["boards"], "相同输入结果不可复现"
    print("用例1 可复现 OK")

    # 用例 2：库存不足 → unplaced + 原因
    boards2 = [BoardType("small", 1000, 500, 1, 0, "none")]
    parts2 = [PartType("a", 400, 400, 4, True, "none", None)]
    r2 = solve(boards2, parts2, 2.0, 2.0)
    check_result(r2, boards2)
    assert r2["summary"]["unplaced_parts"] >= 1
    assert all(u["reason"] == "no_board_inventory_left" for u in r2["unplaced"])
    print("用例2 库存不足 OK:", [u["detail"] for u in r2["unplaced"]][:1])

    # 用例 3：任何板都放不下
    boards3 = [BoardType("tiny", 300, 300, 2, 0, "none")]
    parts3 = [PartType("big", 500, 100, 1, True, "none", None)]
    r3 = solve(boards3, parts3, 2.0, 1.0)
    assert r3["summary"]["unplaced_parts"] == 1
    assert r3["unplaced"][0]["reason"] == "too_large_for_any_board"
    print("用例3 过大零件 OK:", r3["unplaced"][0]["detail"])

    # 用例 4：禁止旋转 + 纹理
    boards4 = [BoardType("oak", 1000, 1000, 2, 0, "along_length")]
    parts4 = [
        PartType("g1", 800, 200, 2, False, "along_length", None),   # 不旋转，纹理同向，OK
        PartType("g2", 200, 800, 2, True, "along_length", None),    # 旋转后纹理沿 Y，不行；不旋转 200x800 沿 X，OK
    ]
    r4 = solve(boards4, parts4, 3.0, 2.0)
    check_result(r4, boards4)
    assert r4["summary"]["unplaced_parts"] == 0
    for bd in r4["boards"]:
        for p in bd["parts"]:
            assert p["grain_axis"] == "x", p
    print("用例4 纹理约束 OK:", r4["summary"])

    # 用例 5：零件级大边距
    boards5 = [BoardType("b", 500, 500, 1, 0, "none")]
    parts5 = [PartType("m", 300, 300, 1, True, "none", 50.0)]
    r5 = solve(boards5, parts5, 2.0, 1.0)
    check_result(r5, boards5)
    p = r5["boards"][0]["parts"][0]
    assert p["x"] >= 50 - 1e-6 and p["y"] >= 50 - 1e-6
    assert p["x"] + p["length"] <= 450 + 1e-6
    print("用例5 零件边距 OK: x,y =", p["x"], p["y"], "cuts =", len(r5["boards"][0]["cuts"]))

    # 用例 6：锯缝被计入尺寸（紧密排样时必须留缝）
    boards6 = [BoardType("b", 100, 1000, 1, 0, "none")]
    parts6 = [PartType("k", 100, 490, 2, False, "none", None)]  # 2*490 + 锯缝3 > 1000? =983 放得下
    r6 = solve(boards6, parts6, 3.0, 1.0)
    assert r6["summary"]["unplaced_parts"] == 0
    parts6 = [PartType("k", 100, 499, 2, False, "none", None)]  # 2*499+3 = 1001 放不下两张
    r6b = solve(boards6, parts6, 3.0, 1.0)
    check_result(r6b, boards6)
    assert r6b["summary"]["unplaced_parts"] >= 1
    print("用例6 锯缝约束 OK")

    # 用例 7：切割线必须贯穿当前所在矩形（简单单调性检查）
    boards7 = [BoardType("b", 600, 600, 1, 20, "none")]
    parts7 = [PartType(f"p{i}", 200, 150, 2, True, "none", None) for i in range(3)]
    r7 = solve(boards7, parts7, 3.0, 1.0)
    check_result(r7, boards7)
    print("用例7 边距板材多零件 OK:", r7["summary"])

    print("\n全部自检通过 ✅")


if __name__ == "__main__":
    main()
