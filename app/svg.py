"""排样结果 SVG 预览。

- 物理坐标原点在板材左下角，Y 轴向上；SVG 中翻转 Y 轴绘制。
- 零件按 id 着色，旋转件用弧形箭头标注；纹理方向用虚线箭头表示。
- 锯切线红色虚线并按切割顺序编号；余料区域灰色虚线描边并标注面积。
"""
from __future__ import annotations

import hashlib

_PALETTE = [
    "#4C9BE8", "#E8964C", "#58B368", "#D26470", "#9B7ED4",
    "#4FB3A9", "#C9A13D", "#6E83B7", "#B0633F", "#7FA85C",
]


def _color(part_id: str) -> str:
    h = int(hashlib.md5(part_id.encode("utf-8")).hexdigest(), 16)
    return _PALETTE[h % len(_PALETTE)]


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_svg(plan: dict) -> str:
    solution = plan["solution"] if "solution" in plan else plan["result"]
    boards = solution["boards"]
    margin_px = 30.0
    gap = 60.0
    header = 46.0

    # 每张板按最长边归一缩放到 420px
    panels = []
    max_h = 0.0
    for b in boards:
        scale = 420.0 / max(b["length"], b["width"])
        pw = b["length"] * scale
        ph = b["width"] * scale
        panels.append((b, scale, pw, ph))
        max_h = max(max_h, ph)

    total_w = margin_px * 2 + sum(pw for _, _, pw, _ in panels) + gap * max(0, len(panels) - 1)
    total_h = margin_px * 2 + header + max_h + 26.0
    if not boards:
        total_w, total_h = 360.0, 150.0

    parts_svg: list[str] = []
    parts_svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" '
        f'viewBox="0 0 {total_w:.0f} {total_h:.0f}" font-family="Arial, sans-serif">'
    )
    parts_svg.append(
        '<rect width="100%" height="100%" fill="white"/>'
        f'<text x="{margin_px}" y="24" font-size="16" font-weight="bold">'
        f'方案 {_esc(plan["plan_id"])} · 利用率 {solution["summary"]["utilization_rate"] * 100:.1f}%'
        f' · 用板 {solution["summary"]["boards_used"]} 张'
        "</text>"
    )

    if not boards:
        parts_svg.append(
            f'<text x="{margin_px}" y="80" font-size="14" fill="#a00">无成功排样的板材（所有零件均未安放）</text>'
        )

    cursor_x = margin_px
    for b, scale, pw, ph in panels:
        ox, oy = cursor_x, header
        # 翻转 Y 轴：物理 (x,y) -> SVG (ox + x*s, oy + (W - y)*s)
        W = b["width"]

        def sx(x: float) -> float:
            return ox + x * scale

        def sy(y: float) -> float:
            return oy + (W - y) * scale

        parts_svg.append(
            f'<text x="{ox:.1f}" y="{oy - 12:.1f}" font-size="13" font-weight="bold">'
            f'#{b["board_no"]} {_esc(b["instance_id"])} ({b["length"]:g}×{b["width"]:g}) '
            f'利用率 {b["utilization_rate"] * 100:.1f}%</text>'
        )
        # 板边框
        parts_svg.append(
            f'<rect x="{ox:.2f}" y="{oy:.2f}" width="{pw:.2f}" height="{ph:.2f}" '
            'fill="#fafafa" stroke="#222" stroke-width="2"/>'
        )
        # 边距框
        m = b.get("margin", 0.0)
        if m > 0:
            parts_svg.append(
                f'<rect x="{sx(m):.2f}" y="{sy(W - m):.2f}" '
                f'width="{(b["length"] - 2 * m) * scale:.2f}" '
                f'height="{(b["width"] - 2 * m) * scale:.2f}" '
                'fill="none" stroke="#999" stroke-width="1" stroke-dasharray="5,4"/>'
            )

        # 余料区域
        for r in b["leftover_regions"]:
            parts_svg.append(
                f'<rect x="{sx(r["x"]):.2f}" y="{sy(r["y"] + r["width"]):.2f}" '
                f'width="{r["length"] * scale:.2f}" height="{r["width"] * scale:.2f}" '
                'fill="#eef1f5" stroke="#b8c0cc" stroke-width="1" stroke-dasharray="3,3"/>'
            )

        # 零件
        for p in b["parts"]:
            color = _color(p["part_id"])
            px, py = sx(p["x"]), sy(p["y"] + p["width"])
            pw_, ph_ = p["length"] * scale, p["width"] * scale
            parts_svg.append(
                f'<rect x="{px:.2f}" y="{py:.2f}" width="{pw_:.2f}" height="{ph_:.2f}" '
                f'fill="{color}" fill-opacity="0.45" stroke="{color}" stroke-width="1.5"/>'
            )
            label = p["part_id"]
            if p["rotated"]:
                label += " ↻90"
            fs = max(8.0, min(13.0, min(pw_, ph_) / max(2.2, len(label) * 0.62)))
            if pw_ > 26 and ph_ > 12:
                parts_svg.append(
                    f'<text x="{px + pw_ / 2:.2f}" y="{py + ph_ / 2 + fs * 0.35:.2f}" '
                    f'font-size="{fs:.1f}" text-anchor="middle" fill="#1a1a1a">{_esc(label)}</text>'
                )
            # 纹理箭头
            if p["grain_axis"] in ("x", "y"):
                if p["grain_axis"] == "x":
                    x1, y1 = px + 5, py + ph_ / 2
                    x2, y2 = px + pw_ - 5, py + ph_ / 2
                else:
                    x1, y1 = px + pw_ / 2, py + ph_ - 5
                    x2, y2 = px + pw_ / 2, py + 5
                parts_svg.append(
                    f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                    'stroke="#333" stroke-width="0.8" stroke-dasharray="2,2" marker-end="url(#arr)"/>'
                )

        # 切割线
        for c in b["cuts"]:
            if c["direction"] == "v":
                x1, y1 = sx(c["position"]), sy(c["start"])
                x2, y2 = sx(c["position"]), sy(c["end"])
            else:
                x1, y1 = sx(c["start"]), sy(c["position"])
                x2, y2 = sx(c["end"]), sy(c["position"])
            parts_svg.append(
                f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                'stroke="#d23" stroke-width="1.1" stroke-dasharray="6,3"/>'
            )
            parts_svg.append(
                f'<circle cx="{x2:.2f}" cy="{y2:.2f}" r="7" fill="#d23"/>'
                f'<text x="{x2:.2f}" y="{y2 + 3:.2f}" font-size="9" fill="white" '
                f'text-anchor="middle" font-weight="bold">{c["seq"]}</text>'
            )

        cursor_x += pw + gap

    parts_svg.insert(
        1,
        '<defs><marker id="arr" markerWidth="7" markerHeight="7" refX="6" refY="3" '
        'orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#333"/></marker></defs>',
    )
    parts_svg.append("</svg>")
    return "\n".join(parts_svg)
