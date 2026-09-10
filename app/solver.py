"""贯穿式直切（guillotine）矩形排样求解器。

约束与建模
----------
- 只允许贯穿当前待分矩形的直切（横切 / 竖切）。每个零件从一个自由矩形的
  左上角切出，最多两刀：
    * 方案 V：整高竖切分离右侧余料 → 在左块横切分离下方余料；
    * 方案 H：整宽横切分离下方余料 → 在上块竖切分离右侧余料。
- 锯缝 ``kerf`` 完全从余料侧扣除（零件保持净尺寸）；窄于锯缝的余料无法
  再切出，视为废料。
- 边距 ``margin``：零件四边距物理板边必须保留的宽度；零件级边距优先于
  板材级。需要边距时零件左上角内移，让出来的边条并入相邻余料，
  后续零件是否能用由其自身边距决定。
- 旋转：零件可旋转 90°；旋转后零件自身纹理方向随之转动，必须与有纹理
  板材的纹理方向平行。
- 优化顺序固定为：① 未安放零件数最少 ② 板材使用张数最少
  ③ 废料面积最小（即废料率最低）。
- 求解确定性：固定策略序列 + 固定随机种子；达到时间上限即返回当前最佳。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional

EPS = 1e-6


# --------------------------------------------------------------------------- #
# 输入实体
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BoardType:
    id: str
    length: float
    width: float
    quantity: int
    margin: float
    grain: str  # "none" | "along_length"(板长X) | "across_length"(板宽Y)


@dataclass(frozen=True)
class PartType:
    id: str
    length: float
    width: float
    quantity: int
    allow_rotation: bool
    grain: str
    margin: Optional[float]


@dataclass(frozen=True)
class PartInstance:
    part: PartType
    seq: int  # 同 id 零件的第几个实例，从 1 开始

    @property
    def uid(self) -> str:
        return f"{self.part.id}#{self.seq}"


# --------------------------------------------------------------------------- #
# 排样过程中的可变结构
# --------------------------------------------------------------------------- #
@dataclass
class Placement:
    uid: str
    part_id: str
    instance_no: int
    x: float
    y: float
    length: float  # 安放后占据 X 方向的尺寸
    width: float
    rotated: bool
    grain_axis: str  # "x" | "y" | "none"


@dataclass(frozen=True)
class CutOp:
    """一次贯穿直切。

    direction = "v"：竖切线 x=position，切口沿 Y 从 start 到 end；
    direction = "h"：横切线 y=position，切口沿 X 从 start 到 end。
    """

    direction: str
    position: float
    start: float
    end: float
    kerf: float
    part_uid: str


@dataclass
class ActiveBoard:
    instance_id: str
    board: BoardType
    free: list[tuple[float, float, float, float]]  # (x, y, w, h)，物理坐标
    placements: list[Placement] = field(default_factory=list)
    cuts: list[CutOp] = field(default_factory=list)
    placed_area: float = 0.0

    @property
    def area(self) -> float:
        return self.board.length * self.board.width


def effective_margin(part: PartType, board: BoardType) -> float:
    return board.margin if part.margin is None else part.margin


def oriented_dims(part: PartType, rotated: bool) -> tuple[float, float]:
    """零件在某朝向下占据的 (X 尺寸, Y 尺寸)。"""
    if rotated:
        return part.width, part.length
    return part.length, part.width


def grain_axis_of(part: PartType, rotated: bool) -> str:
    """零件纹理在板坐标系下的轴向。"""
    if part.grain == "none":
        return "none"
    axis_x = (part.grain == "along_length") != rotated
    return "x" if axis_x else "y"


def orientation_allowed(part: PartType, board: BoardType, rotated: bool) -> bool:
    """朝向是否满足旋转开关与纹理平行约束。"""
    if rotated and not part.allow_rotation:
        return False
    if board.grain == "none" or part.grain == "none":
        return True
    return grain_axis_of(part, rotated) == (
        "x" if board.grain == "along_length" else "y"
    )


# --------------------------------------------------------------------------- #
# 候选安放生成
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    board: ActiveBoard
    rect_index: int
    rect: tuple[float, float, float, float]
    rotated: bool
    split_mode: str  # "V"：先竖后横；"H"：先横后竖
    anchor: tuple[float, float]  # 零件左上角
    new_free: list[tuple[float, float, float, float]]
    new_cuts: list[CutOp]
    kerf_loss: float
    shape_key: tuple  # 余料可再利用性，越大越好


def _make_candidate(
    board: ActiveBoard,
    ri: int,
    rect: tuple[float, float, float, float],
    part: PartType,
    inst: PartInstance,
    rotated: bool,
    kerf: float,
) -> Optional[Candidate]:
    """在给定自由矩形的左上角尝试安放零件，失败返回 None。"""
    x, y, rw, rh = rect
    pw, ph = oriented_dims(part, rotated)
    if pw > rw + EPS or ph > rh + EPS:
        return None

    m = effective_margin(part, board.board)
    L, W = board.board.length, board.board.width

    # 边距只约束物理板边：自由矩形的边距板边已足够远（>m）时，内部那条边
    # 是上一刀的切口，不再要求边距。分别计算四条物理边方向上的可用起点。
    left_gap = x                     # 矩形左边到板左边的距离
    bottom_gap = y
    right_gap = L - (x + rw)         # 矩形右边到板右边的距离
    top_gap = W - (y + rh)
    bx = x if left_gap >= m - EPS else m
    by = y if bottom_gap >= m - EPS else m
    a = bx - x
    t = by - y
    if pw > rw - a + EPS or ph > rh - t + EPS:
        return None
    # 右、上边同样只在触及物理板边时校验
    if right_gap < m - EPS and pw > rw - a - (m - right_gap) + EPS:
        return None
    if top_gap < m - EPS and ph > rh - t - (m - top_gap) + EPS:
        return None

    # 右侧、下方毛间隙；不足锯缝的条料无法切出，按贴边（0）处理
    gx = rw - a - pw
    gy = rh - t - ph
    if gx < -EPS or gy < -EPS:
        return None
    if EPS < gx < kerf - EPS:
        gx = 0.0
    if EPS < gy < kerf - EPS:
        gy = 0.0

    options: list[tuple[str, list, list, float]] = []

    # 方案 V：整高竖切分离右块 → 左块横切分离下方。
    # 毛间隙恰好等于锯缝时净余料为 0：该刀仍需要（切出贴边边界），但不产生余料。
    v_cuts: list[CutOp] = []
    v_free: list[tuple[float, float, float, float]] = []
    v_loss = 0.0
    if gx > EPS:
        v_cuts.append(CutOp("v", bx + pw + kerf / 2, y, y + rh, kerf, inst.uid))
        if gx - kerf > EPS:
            v_free.append((bx + pw + kerf, y, gx - kerf, rh))
        v_loss += kerf * rh
    if gy > EPS:
        v_cuts.append(CutOp("h", by + ph + kerf / 2, bx, bx + pw, kerf, inst.uid))
        if gy - kerf > EPS:
            v_free.append((bx, by + ph + kerf, pw, gy - kerf))
        v_loss += kerf * pw
    options.append(("V", v_cuts, v_free, v_loss))

    # 方案 H：整宽横切分离下块 → 上块竖切分离右侧
    h_cuts: list[CutOp] = []
    h_free: list[tuple[float, float, float, float]] = []
    h_loss = 0.0
    if gy > EPS:
        h_cuts.append(CutOp("h", by + ph + kerf / 2, x, x + rw, kerf, inst.uid))
        if gy - kerf > EPS:
            h_free.append((x, by + ph + kerf, rw, gy - kerf))
        h_loss += kerf * rw
    if gx > EPS:
        h_cuts.append(CutOp("v", bx + pw + kerf / 2, by, by + ph, kerf, inst.uid))
        if gx - kerf > EPS:
            h_free.append((bx + pw + kerf, by, gx - kerf, ph))
        h_loss += kerf * ph
    options.append(("H", h_cuts, h_free, h_loss))

    best: Optional[Candidate] = None
    for mode, cuts, free, kerf_loss in options:
        free = [f for f in free if f[2] > EPS and f[3] > EPS]
        areas = sorted((f[2] * f[3] for f in free), reverse=True)
        sides = sorted((min(f[2], f[3]), max(f[2], f[3])) for f in free)
        shape = tuple(v for pair in reversed(sides) for v in pair)
        cand = Candidate(
            board, ri, rect, rotated, mode, (bx, by), free, cuts, kerf_loss, shape
        )
        # 评分顺序（best-fit 思想）：
        # 1) 最大余料块面积尽量大（保留能容纳后续大件的整块，避免把板切碎成细条；
        #    锯缝损耗的差异已经隐含在保留面积里）；
        # 2) 锯缝材料损耗最小；3) 余料形状尽量方正；4) V/H 切分顺序稳定取值。
        key = (
            tuple(-round(a, 3) for a in areas),
            round(kerf_loss, 6),
            tuple(-s for s in shape),
            mode,
        )
        if best is None:
            best = cand
            best_key = key
        elif key < best_key:
            best, best_key = cand, key
    return best


def _apply_candidate(cand: Candidate, inst: PartInstance) -> None:
    bx, by = cand.anchor
    pw, ph = oriented_dims(inst.part, cand.rotated)
    cand.board.free.pop(cand.rect_index)
    cand.board.free.extend(cand.new_free)
    cand.board.cuts.extend(cand.new_cuts)
    cand.board.placements.append(
        Placement(
            uid=inst.uid,
            part_id=inst.part.id,
            instance_no=inst.seq,
            x=bx,
            y=by,
            length=pw,
            width=ph,
            rotated=cand.rotated,
            grain_axis=grain_axis_of(inst.part, cand.rotated),
        )
    )
    cand.board.placed_area += pw * ph


# --------------------------------------------------------------------------- #
# 单次确定性贪心排样
# --------------------------------------------------------------------------- #
def _open_board(
    boards_def: list[BoardType],
    inventory_used: dict[str, int],
    inst: PartInstance,
    kerf: float,
    prefer_large: bool,
) -> Optional[ActiveBoard]:
    """开一张能容纳该零件的新板；优先最小（或最大）可用规格。"""
    fitting = []
    for b in boards_def:
        if inventory_used[b.id] >= b.quantity:
            continue
        root = (b.margin, b.margin, b.length - 2 * b.margin, b.width - 2 * b.margin)
        for rotated in (False, True):
            if orientation_allowed(inst.part, b, rotated):
                probe = ActiveBoard("__probe__", b, [root])
                if _make_candidate(probe, 0, root, inst.part, inst, rotated, kerf):
                    fitting.append(b)
                    break
    if not fitting:
        return None
    fitting.sort(
        key=lambda b: (b.length * b.width, b.length, b.width, b.id),
        reverse=prefer_large,
    )
    chosen = fitting[0]
    inventory_used[chosen.id] += 1
    idx = inventory_used[chosen.id]
    root = (
        chosen.margin,
        chosen.margin,
        chosen.length - 2 * chosen.margin,
        chosen.width - 2 * chosen.margin,
    )
    return ActiveBoard(f"{chosen.id}#{idx}", chosen, [root])


POLICIES = ("balanced", "no_rotate", "min_kerf", "tight", "h_split")


def _candidate_key(
    cand: Candidate, rect: tuple[float, float, float, float], policy: str
):
    """不同候选选择策略下的排序键（越小越优先）。

    单步贪心无法预见后续零件，因此主循环对多种策略逐一尝试，再按
    「未安放数 → 板材数 → 废料面积」挑选全局最佳。所有策略均为确定性规则。
    """
    rx, ry, rw, rh = rect
    areas = sorted(
        (f[2] * f[3] for f in cand.new_free if f[2] > EPS and f[3] > EPS),
        reverse=True,
    )
    big = -areas[0] if areas else 0.0
    loss = round(cand.kerf_loss, 6)
    shape = tuple(-s for s in cand.shape_key)
    tail = (cand.split_mode, -cand.rect_index, rx, ry)
    if policy == "no_rotate":
        return (cand.rotated, big, loss, shape, *tail)
    if policy == "min_kerf":
        return (loss, big, shape, cand.rotated, *tail)
    if policy == "tight":
        return (rw * rh, big, loss, shape, cand.rotated, *tail)
    if policy == "h_split":
        return (0 if cand.split_mode == "H" else 1, big, loss, shape, cand.rotated, *tail)
    # balanced（默认）：优先保留最大整块余料
    return (big, loss, shape, cand.rotated, *tail)


def _find_best_candidate(
    active: list[ActiveBoard],
    inst: PartInstance,
    kerf: float,
    policy: str = "balanced",
) -> Optional[Candidate]:
    best: Optional[Candidate] = None
    best_key = None
    for board in active:
        # 倒序枚举，弹出索引保持有效；排序键含 -ri 消除枚举顺序影响
        for ri in range(len(board.free) - 1, -1, -1):
            rect = board.free[ri]
            for rotated in (False, True):
                if not orientation_allowed(inst.part, board.board, rotated):
                    continue
                cand = _make_candidate(board, ri, rect, inst.part, inst, rotated, kerf)
                if cand is None:
                    continue
                key = _candidate_key(cand, rect, policy)
                if best_key is None or key < best_key:
                    best_key, best = key, cand
    return best


def pack_order(
    order: list[PartInstance],
    boards_def: list[BoardType],
    kerf: float,
    prefer_large_board: bool,
    policy: str = "balanced",
) -> tuple[list[ActiveBoard], list[PartInstance]]:
    """按给定零件顺序与候选选择策略贪心排样。"""
    active: list[ActiveBoard] = []
    inventory_used: dict[str, int] = {b.id: 0 for b in boards_def}
    unplaced: list[PartInstance] = []

    for inst in order:
        cand = _find_best_candidate(active, inst, kerf, policy)
        if cand is None:
            nb = _open_board(
                boards_def, inventory_used, inst, kerf, prefer_large_board
            )
            if nb is None:
                unplaced.append(inst)
                continue
            active.append(nb)
            cand = _find_best_candidate(active, inst, kerf, policy)
            if cand is None:  # 理论上不会发生（开板前已探测可行）
                unplaced.append(inst)
                continue
        _apply_candidate(cand, inst)

    active.sort(key=lambda bd: (bd.board.length * bd.board.width, bd.instance_id))
    return active, unplaced


# --------------------------------------------------------------------------- #
# 零件排序策略（固定序列，保证可复现）
# --------------------------------------------------------------------------- #
def _build_orders(instances: list[PartInstance]) -> list[list[PartInstance]]:
    p = lambda i: i.part  # noqa: E731
    longest = lambda i: max(p(i).length, p(i).width)  # noqa: E731
    shortest = lambda i: min(p(i).length, p(i).width)  # noqa: E731
    area = lambda i: p(i).length * p(i).width  # noqa: E731
    bases = [
        sorted(instances, key=lambda i: (-longest(i), -area(i), p(i).id, i.seq)),
        sorted(instances, key=lambda i: (-area(i), p(i).id, i.seq)),
        sorted(instances, key=lambda i: (-p(i).length, -p(i).width, p(i).id, i.seq)),
        sorted(instances, key=lambda i: (-p(i).width, -p(i).length, p(i).id, i.seq)),
        sorted(instances, key=lambda i: (longest(i), -area(i), p(i).id, i.seq)),
        sorted(instances, key=lambda i: (area(i), p(i).id, i.seq)),
        sorted(instances, key=lambda i: (-(p(i).length + p(i).width), p(i).id, i.seq)),
        sorted(
            instances,
            key=lambda i: (-(longest(i) / max(shortest(i), EPS)), -area(i), p(i).id, i.seq),
        ),
        # 受约束（不允许旋转/有纹理）的大件优先
        sorted(
            instances,
            key=lambda i: (
                p(i).allow_rotation and p(i).grain == "none",
                -longest(i),
                p(i).id,
                i.seq,
            ),
        ),
        # 越方正的优先
        sorted(
            instances,
            key=lambda i: (
                -(shortest(i) / max(longest(i), EPS)),
                -area(i),
                p(i).id,
                i.seq,
            ),
        ),
        # 越细长的优先
        sorted(
            instances,
            key=lambda i: (
                shortest(i) / max(longest(i), EPS),
                -area(i),
                p(i).id,
                i.seq,
            ),
        ),
        sorted(instances, key=lambda i: (-area(i), -longest(i), p(i).id, i.seq)),
    ]
    return bases


# --------------------------------------------------------------------------- #
# 求解入口
# --------------------------------------------------------------------------- #
def solve(
    boards_def: list[BoardType],
    parts_def: list[PartType],
    kerf: float,
    time_limit: float,
) -> dict:
    """多起点搜索；返回可直接 JSON 序列化的方案结果。"""
    start = time.monotonic()
    deadline = start + max(0.01, time_limit)

    instances: list[PartInstance] = []
    for pt in sorted(parts_def, key=lambda p: p.id):
        for s in range(1, pt.quantity + 1):
            instances.append(PartInstance(pt, s))

    total_demand_area = sum(i.part.length * i.part.width for i in instances)

    # 预检查：在任何全新板材上都放不下的零件（尺寸/边距/锯缝/纹理原因）
    def fits_any_fresh(inst: PartInstance) -> set[str]:
        ok = set()
        for b in boards_def:
            root = (
                b.margin,
                b.margin,
                b.length - 2 * b.margin,
                b.width - 2 * b.margin,
            )
            for rotated in (False, True):
                if orientation_allowed(inst.part, b, rotated):
                    probe = ActiveBoard("__probe__", b, [root])
                    if _make_candidate(probe, 0, root, inst.part, inst, rotated, kerf):
                        ok.add(b.id)
                        break
        return ok

    never_fits = {i.uid: fits_any_fresh(i) for i in instances}

    orders = _build_orders(instances)
    # 确定性网格：零件顺序 × 开板偏好 × 候选选择策略
    deterministic_grid = [
        (order, prefer_large, policy)
        for order in orders
        for prefer_large in (False, True)
        for policy in POLICIES
    ]
    rng = random.Random(0xC0FFEE)

    best: Optional[tuple[list[ActiveBoard], list[PartInstance]]] = None
    best_key = None
    strategies_tried = 0
    timed_out = False
    random_runs = 0

    def consider(result: tuple[list[ActiveBoard], list[PartInstance]]) -> None:
        nonlocal best, best_key
        active_boards, unplaced_parts = result
        waste = sum(b.area for b in active_boards) - sum(
            b.placed_area for b in active_boards
        )
        # 优化顺序：未安放数 → 板材数 → 废料面积
        key = (len(unplaced_parts), len(active_boards), round(waste, 6))
        if best_key is None or key < best_key:
            best_key, best = key, result

    while True:
        if strategies_tried < len(deterministic_grid):
            # 阶段一：确定性网格；预算紧张时允许提前退出到 best-effort
            if strategies_tried > 0 and time.monotonic() >= deadline:
                timed_out = True
                break
            order, prefer_large, policy = deterministic_grid[strategies_tried]
        else:
            # 阶段二：剩余时间内用固定种子随机扰动顺序继续搜索
            if time.monotonic() >= deadline or random_runs >= 2_000:
                timed_out = time.monotonic() >= deadline
                break
            order = list(instances)
            rng.shuffle(order)
            prefer_large = rng.random() < 0.5
            policy = POLICIES[rng.randrange(len(POLICIES))]
            random_runs += 1

        consider(pack_order(order, boards_def, kerf, prefer_large, policy))
        strategies_tried += 1

    assert best is not None
    active, unplaced = best

    inventory_left = {
        b.id: b.quantity - sum(1 for bd in active if bd.board.id == b.id)
        for b in boards_def
    }
    unplaced_entries = _classify_unplaced(
        unplaced, never_fits, inventory_left
    )

    elapsed = time.monotonic() - start
    complete = not unplaced
    if complete and not timed_out:
        status = "optimal_heuristic"          # 预算内完成全部搜索
    elif complete:
        status = "complete_timeout"           # 已全部安放，但预算耗尽停止继续优化
    elif timed_out:
        status = "best_effort_timeout"        # 预算耗尽，返回当前最佳（有未安放件）
    else:
        status = "incomplete"                 # 预算用完仍有未安放件（多为库存不足）

    return _serialize(
        active,
        unplaced_entries,
        total_demand_area,
        kerf,
        status,
        strategies_tried,
        elapsed,
        timed_out,
    )


def _classify_unplaced(
    unplaced: list[PartInstance],
    never_fits: dict[str, set[str]],
    inventory_left: dict[str, int],
) -> list[dict]:
    entries: list[dict] = []
    for inst in unplaced:
        fitting = never_fits[inst.uid]
        m_desc = (
            f"（零件边距 {inst.part.margin:g}）"
            if inst.part.margin is not None
            else ""
        )
        if not fitting:
            grain_hint = ""
            if not inst.part.allow_rotation and inst.part.grain != "none":
                grain_hint = "；同时禁止旋转且纹理要求与所有有纹理板材方向正交"
            reason = "too_large_for_any_board"
            detail = (
                f"零件 {inst.part.length:g}×{inst.part.width:g}{m_desc} 在考虑锯缝、边距、"
                f"旋转开关与纹理方向后，无法放入任何规格的整张板材{grain_hint}"
            )
        else:
            exhausted = [bid for bid in fitting if inventory_left[bid] <= 0]
            reason = "no_board_inventory_left"
            detail = (
                f"可放入规格 {sorted(fitting)}，但这些板材库存已用完"
                + (
                    f"（耗尽：{sorted(exhausted)}）"
                    if exhausted
                    else "；现有板材余料空间也无法容纳"
                )
            )
        entries.append(
            {
                "part_id": inst.part.id,
                "instance_no": inst.seq,
                "length": inst.part.length,
                "width": inst.part.width,
                "reason": reason,
                "detail": detail,
            }
        )
    entries.sort(key=lambda e: (e["part_id"], e["instance_no"]))
    return entries


def _r(v: float) -> float:
    return round(float(v), 3)


def _serialize(
    active: list[ActiveBoard],
    unplaced_entries: list[dict],
    total_demand_area: float,
    kerf: float,
    status: str,
    strategies_tried: int,
    elapsed: float,
    timed_out: bool,
) -> dict:
    boards_out = []
    total_board_area = 0.0
    total_placed_area = 0.0
    for bi, bd in enumerate(active, start=1):
        cuts = [
            {
                "seq": n,
                "direction": op.direction,
                "position": _r(op.position),
                "start": _r(op.start),
                "end": _r(op.end),
                "kerf": _r(op.kerf),
                "after_part": op.part_uid,
            }
            for n, op in enumerate(bd.cuts, start=1)
        ]
        leftovers = [
            {
                "x": _r(x),
                "y": _r(y),
                "length": _r(w),
                "width": _r(h),
                "area": _r(w * h),
            }
            for (x, y, w, h) in sorted(
                bd.free, key=lambda f: (-f[2] * f[3], f[0], f[1])
            )
            if w > EPS and h > EPS
        ]
        placements = [
            {
                "part_id": pl.part_id,
                "instance_no": pl.instance_no,
                "x": _r(pl.x),
                "y": _r(pl.y),
                "length": _r(pl.length),
                "width": _r(pl.width),
                "rotated": pl.rotated,
                "grain_axis": pl.grain_axis,
            }
            for pl in sorted(bd.placements, key=lambda p: (p.y, p.x))
        ]
        area = bd.area
        total_board_area += area
        total_placed_area += bd.placed_area
        boards_out.append(
            {
                "board_no": bi,
                "instance_id": bd.instance_id,
                "board_id": bd.board.id,
                "length": _r(bd.board.length),
                "width": _r(bd.board.width),
                "margin": _r(bd.board.margin),
                "grain": bd.board.grain,
                "parts": placements,
                "cuts": cuts,
                "leftover_regions": leftovers,
                "used_area": _r(bd.placed_area),
                "utilization_rate": round(bd.placed_area / area, 6),
            }
        )

    utilization = total_placed_area / total_board_area if total_board_area else 0.0
    waste_rate = 1.0 - utilization if total_board_area else 0.0
    return {
        "status": status,
        "timed_out": timed_out,
        "strategies_tried": strategies_tried,
        "elapsed_seconds": round(elapsed, 6),
        "kerf": kerf,
        "summary": {
            "boards_used": len(active),
            "total_board_area": _r(total_board_area),
            "total_demand_area": _r(total_demand_area),
            "placed_area": _r(total_placed_area),
            "waste_area": _r(total_board_area - total_placed_area),
            "utilization_rate": round(utilization, 6),
            "waste_rate": round(waste_rate, 6),
            "placed_parts": sum(len(b.placements) for b in active),
            "unplaced_parts": len(unplaced_entries),
        },
        "boards": boards_out,
        "unplaced": unplaced_entries,
    }
