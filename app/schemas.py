"""请求/响应数据模型与输入校验。

校验分两层：
1. Pydantic 负责字段级约束（非法尺寸、非正数量等），错误自带字段路径；
2. `semantic_validate` 负责跨字段语义校验（重复标识、旋转/纹理矛盾等），
   同样以字段路径形式抛出统一格式的 422 错误。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# 业务上下限，防止资源耗尽型请求
MAX_DIMENSION = 100_000.0
MAX_QUANTITY = 10_000
MAX_LIST_ITEMS = 200
MAX_TIME_LIMIT = 60.0


class Grain(str, Enum):
    """纹理方向。

    统一以「部件未旋转时的长边沿 X 方向」为参照描述纹理：
    - NONE: 无纹理要求；
    - ALONG_LENGTH: 纹理沿长度方向（未旋转时为 X）；
    - ACROSS_LENGTH: 纹理垂直于长度方向（未旋转时为 Y）。
    """

    NONE = "none"
    ALONG_LENGTH = "along_length"
    ACROSS_LENGTH = "across_length"


class BoardSpec(BaseModel):
    """板材规格与库存数量。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, description="板材规格标识")
    length: float = Field(..., gt=0, le=MAX_DIMENSION, description="板材长度（X 方向）")
    width: float = Field(..., gt=0, le=MAX_DIMENSION, description="板材宽度（Y 方向）")
    quantity: int = Field(..., gt=0, le=MAX_QUANTITY, description="可用数量（必须为正）")
    margin: float = Field(
        0.0, ge=0, lt=MAX_DIMENSION, description="该板材默认边距（四周需保留的边条宽度）"
    )
    grain: Grain = Field(
        Grain.NONE, description="板材纹理方向（沿板长 X / 沿板宽 Y / 无）"
    )

    @field_validator("id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("标识不能为空字符串")
        return v


class PartSpec(BaseModel):
    """零件需求。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, description="零件标识")
    length: float = Field(..., gt=0, le=MAX_DIMENSION, description="零件长度（未旋转时沿 X）")
    width: float = Field(..., gt=0, le=MAX_DIMENSION, description="零件宽度（未旋转时沿 Y）")
    quantity: int = Field(..., gt=0, le=MAX_QUANTITY, description="需求数量（必须为正）")
    allow_rotation: bool = Field(True, description="是否允许旋转 90 度")
    grain: Grain = Field(
        Grain.NONE, description="零件纹理要求（相对零件自身未旋转的长度方向）"
    )
    margin: Optional[float] = Field(
        None, ge=0, lt=MAX_DIMENSION, description="零件级边距，缺省时使用板材边距"
    )

    @field_validator("id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("标识不能为空字符串")
        return v


class PlanRequest(BaseModel):
    """创建/重新求解方案的请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, max_length=120, description="方案名称（不参与幂等键）")
    kerf: float = Field(
        3.0, ge=0, le=1_000.0, description="锯缝宽度（每次直切损失的材料宽度）"
    )
    time_limit: float = Field(
        5.0, gt=0, le=MAX_TIME_LIMIT, description="最大计算时长（秒），超时返回当前最佳"
    )
    boards: list[BoardSpec] = Field(..., min_length=1, max_length=MAX_LIST_ITEMS)
    parts: list[PartSpec] = Field(..., min_length=1, max_length=MAX_LIST_ITEMS)


class SemanticError(Exception):
    """跨字段语义校验错误，字段路径格式与 Pydantic 保持一致。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"{len(errors)} 个语义校验错误")


def _err(loc: tuple, msg: str, etype: str = "value_error") -> dict:
    return {"loc": list(loc), "msg": msg, "type": etype}


def semantic_validate(req: PlanRequest) -> None:
    """跨字段校验，失败时抛出 SemanticError（可一次返回全部错误）。"""
    errors: list[dict] = []

    # 1. 重复标识：板材/零件各自不能重复，二者之间也不能重名
    seen: dict[str, str] = {}

    def _check_id(kind: str, index: int, identifier: str) -> None:
        loc = (kind, index, "id")
        if identifier in seen:
            other = seen[identifier]
            if other == kind:
                errors.append(_err(loc, f"标识 {identifier!r} 在{kind}列表中重复", "duplicate_id"))
            else:
                errors.append(
                    _err(
                        loc,
                        f"标识 {identifier!r} 与{other}列表中的标识重复",
                        "duplicate_id",
                    )
                )
        else:
            seen[identifier] = kind

    for i, b in enumerate(req.boards):
        _check_id("boards", i, b.id)
        # 边距大到连可排样区域都不存在
        if b.margin > 0 and (2 * b.margin >= b.length or 2 * b.margin >= b.width):
            errors.append(
                _err(
                    ("boards", i, "margin"),
                    f"边距 {b.margin:g} 过大：板材 {b.length:g}×{b.width:g} "
                    f"扣除四周边距后无可用区域",
                    "margin_too_large",
                )
            )

    for i, p in enumerate(req.parts):
        _check_id("parts", i, p.id)

        if p.margin is not None and (
            2 * p.margin >= p.length or 2 * p.margin >= p.width
        ):
            errors.append(
                _err(
                    ("parts", i, "margin"),
                    f"边距 {p.margin:g} 过大：零件 {p.length:g}×{p.width:g} "
                    f"四周无法同时保留该边距",
                    "margin_too_large",
                )
            )

        # 2. 旋转/纹理矛盾：禁止旋转，且纹理要求与有纹理的板材方向正交，
        #    则不存在任何可行朝向（未旋转就与板材垂直，旋转后自身纹理又不允许）。
        if not p.allow_rotation and p.grain != Grain.NONE:
            for j, b in enumerate(req.boards):
                if b.grain == Grain.NONE:
                    continue
                # 未旋转时零件纹理轴向（X=沿长 / Y=垂直于长）
                part_axis_x = p.grain == Grain.ALONG_LENGTH
                board_axis_x = b.grain == Grain.ALONG_LENGTH
                if part_axis_x != board_axis_x:
                    errors.append(
                        _err(
                            ("parts", i),
                            f"旋转/纹理设置相互矛盾：零件 {p.id!r} 禁止旋转且纹理为 "
                            f"{p.grain.value}，在板材 {b.id!r}（纹理 {b.grain.value}）"
                            "上永远无法与板材纹理同向；允许旋转或修正纹理方向后重试",
                            "grain_rotation_conflict",
                        )
                    )
                    break

    if errors:
        raise SemanticError(errors)
